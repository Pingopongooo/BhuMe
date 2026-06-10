"""
Worked end-to-end example — load → look → predict → score.

Main orchestrator script linking independent processing modules from bhume.baseline.
Ensures all metrics are evaluated in metric UTM coordinates while exporting a 
standardized EPSG:4326 FeatureCollection.

Run:
    uv run quickstart.py data/34855_vadnerbhairav_chandavad_nashik
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import rasterio
import geopandas as gpd
import matplotlib.pyplot as plt
from shapely.affinity import translate

from bhume import load, patch_for_plot, score, write_predictions
from bhume.baseline import global_median_shift, edge_map, templates, calculate_alignment_shift, save_validation_overlay
from bhume.geo import open_imagery

DEFAULT_VILLAGE = 'data/34855_vadnerbhairav_chandavad_nashik'


def main(village_dir: str) -> None:
    village = load(village_dir)

    # Compute village UTM once — all plots share the same zone
    village_utm = gpd.GeoSeries([village.plots.iloc[0].geometry], crs="EPSG:4326").estimate_utm_crs()

    # --- PRE-LOOP: COMPUTE PER-VILLAGE FLAGGING THRESHOLDS ---
    all_area_ratios = []
    all_compactness = []

    for pn in village.plots.index:
        plot_row = village.plots.loc[pn]
        g = gpd.GeoSeries([village.plots.loc[pn].geometry], crs="EPSG:4326").to_crs(village_utm).iloc[0]

        area = g.area
        perim = g.length
        recorded = (plot_row.get("recorded_area_sqm") or 0) + (plot_row.get("pot_kharaba_ha") or 0) * 10_000

        if recorded > 0:
            all_area_ratios.append(area / recorded)
        if area > 0 and perim > 0:
            all_compactness.append((4 * np.pi * area) / (perim ** 2))

    if all_area_ratios:
        ar_mean, ar_std = np.mean(all_area_ratios), np.std(all_area_ratios)
        area_low  = max(0.3, ar_mean - 3 * ar_std)
        area_high = ar_mean + 3 * ar_std
    else:
        area_low, area_high = 0.60, 1.60

    if all_compactness:
        comp_mean, comp_std = np.mean(all_compactness), np.std(all_compactness)
        compactness_threshold = max(0.01, comp_mean - 3 * comp_std)
    else:
        compactness_threshold = 0.05

    print(f"Village thresholds — Area ratio: [{area_low:.2f}, {area_high:.2f}] | Compactness min: {compactness_threshold:.4f}")

    results = []
    flagged_count = 0
    aligned_count = 0

    for plot_id, master_canvas, satellite_bg, spatial_meta in edge_map(village, pad_m=30):
        print(f"\n--- Checking Plot ID: {plot_id} ---")

        pn = spatial_meta["pn"]
        plot_row = spatial_meta["plot_row"]
        plot_geom_4326 = village.plots.loc[pn].geometry

        geom_series_4326 = gpd.GeoSeries([plot_geom_4326], crs="EPSG:4326")
        geom_metric = geom_series_4326.to_crs(village_utm).iloc[0]

        vector_area = geom_metric.area
        vector_perimeter = geom_metric.length

        recorded_area = (plot_row.get("recorded_area_sqm") or 0)
        total_legal_area = recorded_area + (plot_row.get("pot_kharaba_ha") or 0) * 10_000

        # --- FLAG A: SIZE MISMATCH ---
        if total_legal_area > 0:
            area_ratio = vector_area / total_legal_area
            if not (area_low <= area_ratio <= area_high):
                print(f" ⚠️ [FLAGGED] Plot {plot_id}: Size Mismatch ({area_ratio:.2f}x)")
                results.append({"plot_number": str(plot_id), "status": "flagged", "confidence": 0.0, "geometry": plot_geom_4326})
                flagged_count += 1
                continue

        # --- FLAG B: SHAPE DEFORMATION ---
        if vector_area > 0 and vector_perimeter > 0:
            compactness = (4 * np.pi * vector_area) / (vector_perimeter ** 2)
            if compactness < compactness_threshold:
                print(f" ⚠️ [FLAGGED] Plot {plot_id}: Corrupted Shape")
                results.append({"plot_number": str(plot_id), "status": "flagged", "confidence": 0.0, "geometry": plot_geom_4326})
                flagged_count += 1
                continue

        # --- ALIGNMENT ---
        print(f" ✅ [PASSED] Plot {plot_id}. Running alignment...")

        tight_stencil, anchor_x, anchor_y = templates(master_canvas, spatial_meta, plot_id=plot_id)

        dx_px, dy_px, dx_m, dy_m, confidence, _ = calculate_alignment_shift(
            master_canvas, tight_stencil, anchor_x, anchor_y, spatial_meta, plot_id=plot_id
        )

        shifted_geom = gpd.GeoSeries(
            [translate(geom_series_4326.to_crs(village_utm).iloc[0], dx_m, dy_m)], crs=village_utm
        ).to_crs("EPSG:4326").iloc[0]

        results.append({
            "plot_number": str(plot_id),
            "status": "corrected",
            "confidence": round(float(np.clip(confidence, 0.0, 1.0)), 4),
            "geometry": shifted_geom,
        })
        aligned_count += 1

        save_validation_overlay(satellite_bg, spatial_meta, dx_px, dy_px, plot_id, village)

    # --- SUMMARY ---
    print(f"\n{'='*50}")
    print(f" Aligned: {aligned_count}  |  Flagged: {flagged_count}")
    print(f"{'='*50}\n")

    preds_gdf = gpd.GeoDataFrame(results, geometry="geometry", crs="EPSG:4326")
    out = write_predictions(Path(village_dir) / 'predictions.geojson', preds_gdf)
    print(f"Saved -> {out}")

    if village.example_truths is None:
        return

    print(f"\n{'-'*60}\n{score(preds_gdf, village)}\n{'-'*60}")

    # --- PER-PLOT DRILLDOWN ---
    truth_utm = village.example_truths.to_crs(village_utm)
    pred_utm = preds_gdf.copy()
    pred_utm['plot_number'] = pred_utm['plot_number'].astype(str)
    pred_utm = pred_utm.set_index('plot_number').to_crs(village_utm)

    print(f"\n{'='*65}")
    print(f"{'Plot ID':<10} | {'Status':<10} | {'Confidence':<12} | {'IoU':<10}")
    print(f"{'-'*65}")

    for pn in village.example_truths.index:
        pn_str = str(pn)
        if pn_str not in pred_utm.index:
            continue

        row = pred_utm.loc[pn_str]
        status = row.get('status', 'unknown')
        conf = row.get('confidence', 0.0)

        if status != 'corrected':
            print(f"{pn_str:<10} | {status:<10} | {conf:<12.4f} | {'N/A (Skipped)'}")
            continue

        geom_p, geom_t = row['geometry'], truth_utm.loc[pn, 'geometry']
        union_area = geom_p.union(geom_t).area
        iou = geom_p.intersection(geom_t).area / union_area if union_area > 0 else 0.0
        print(f"{pn_str:<10} | {status:<10} | {conf:<12.4f} | {iou:<10.4f}")

    print(f"{'='*65}\n")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VILLAGE)