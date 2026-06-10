"""A deliberately naive baseline — a floor to beat, and a worked load→predict→score loop."""

from __future__ import annotations

import statistics

import geopandas as gpd
from shapely.affinity import translate
import cv2
import numpy as np
from PIL import Image
import rasterio
import geopandas as gpd
import matplotlib.pyplot as plt
from bhume.geo import open_imagery


# def _utm_for(geom) -> str:
#     lon = geom.centroid.x
#     return f'EPSG:{32600 + int((lon + 180) // 6) + 1}'


# def global_median_shift(village, confidence: float = 0.5) -> gpd.GeoDataFrame:
#     """Estimate ONE translation from the example truths and apply it to every plot.

#     The error is mostly a coherent per-village offset, so a single shift helps a lot of plots —
#     and visibly misses the ones whose drift differs (rotation, local stretch, outliers). That gap
#     is the interesting part this baseline leaves for you. Returns a contract-shaped predictions
#     GeoDataFrame (all `corrected`, uniform `confidence` — note how flat confidence tanks the
#     calibration score).
#     """
#     if village.example_truths is None:
#         raise ValueError(f'{village.slug} has no example_truths.geojson to estimate a shift from')

    # utm = _utm_for(village.example_truths.geometry.iloc[0])
    # official_u = village.plots.to_crs(utm)
    # truth_u = village.example_truths.to_crs(utm)

    # dxs, dys = [], []
    # for pn in village.example_truths.index:
    #     if pn in official_u.index:
    #         o = official_u.loc[pn, 'geometry'].centroid
    #         t = truth_u.loc[pn, 'geometry'].centroid
    #         dxs.append(t.x - o.x)
    #         dys.append(t.y - o.y)
    # if not dxs:
    #     raise ValueError('no overlapping plots between example truths and the cadastre')
    # mdx, mdy = statistics.median(dxs), statistics.median(dys)

    # shifted = official_u.copy()
    # shifted['geometry'] = shifted.geometry.apply(lambda g: translate(g, mdx, mdy))
    # preds = shifted.to_crs('EPSG:4326')
    # preds['status'] = 'corrected'
    # preds['confidence'] = confidence
    # preds['method_note'] = f'global median shift dx={mdx:.1f}m dy={mdy:.1f}m'
    # return preds[['plot_number', 'status', 'confidence', 'method_note', 'geometry']]

    # My custom logic here
def edge_map(village, pad_m=30):
    """Yields the plot ID, binarized boundary hint patch, RGB image patch, and 
    associated spatial metadata for every plot within a specified village.
    """
    with open_imagery(village.imagery_path) as img_src, rasterio.open(village.boundaries_path) as hint_src:
        img_crs = img_src.crs
        img_transform = img_src.transform
        img_res = img_src.res

        for pn in village.plots.index:
            plot_row = village.plots.loc[pn]
            plot_id = plot_row['plot_number']

            geom_proj = gpd.GeoSeries([village.plot(pn)], crs="EPSG:4326").to_crs(img_crs).iloc[0]

            minx, miny, maxx, maxy = geom_proj.bounds
            minx -= pad_m; miny -= pad_m; maxx += pad_m; maxy += pad_m

            full_img_window = rasterio.windows.Window(0, 0, img_src.width, img_src.height)
            full_hint_window = rasterio.windows.Window(0, 0, hint_src.width, hint_src.height)

            img_window = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=img_transform).intersection(full_img_window)
            hint_window = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=hint_src.transform).intersection(full_hint_window)

            geom_w = (maxx - minx)  
            geom_h = (maxy - miny)  
            template_w_px = geom_w / img_src.res[0]
            template_h_px = geom_h / img_src.res[1]

            min_search_w = template_w_px * 0.7  
            min_search_h = template_h_px * 0.7

            if (img_window.width < 4 or img_window.height < 4 or
                hint_window.width < 4 or hint_window.height < 4 or
                img_window.width < min_search_w or img_window.height < min_search_h):
                print(f" ⚠️ [SKIPPED] Plot {plot_id}: Window too small after raster boundary clamp")
                continue

            rgb_image = np.moveaxis(img_src.read([1, 2, 3], window=img_window), 0, -1)
            hint_patch = hint_src.read(1, window=hint_window)

            actual_transform = rasterio.windows.transform(img_window, img_transform)

            hint_bin = (hint_patch * 255).astype(np.uint8) if hint_patch.max() <= 1.0 else hint_patch.astype(np.uint8)
            _, hint_bin = cv2.threshold(hint_bin, 127, 255, cv2.THRESH_BINARY)

            if hint_bin.shape != rgb_image.shape[:2]:
                hint_bin = cv2.resize(hint_bin, (rgb_image.shape[1], rgb_image.shape[0]), interpolation=cv2.INTER_NEAREST)

            yield plot_id, hint_bin, rgb_image, {
                "pn": pn,
                "plot_row": plot_row,
                "geom_proj": geom_proj,
                "patch_minx": actual_transform.c,
                "patch_maxy": actual_transform.f,
                "img_res": img_res,
                "img_crs": img_crs,
            }


def _geom_to_pixels(geom_proj, patch_minx, patch_maxy, img_res):
    """Converts a projected spatial polygon's exterior coordinates into local pixel coordinates 
    relative to the bounding extents of the image patch.
    """
    polys = [geom_proj] if geom_proj.geom_type == 'Polygon' else list(geom_proj.geoms)
    result = []
    for poly in polys:
        pixels = np.array([
            [(x - patch_minx) / img_res[0], (patch_maxy - y) / img_res[1]]
            for x, y in poly.exterior.coords
        ], dtype=np.int32)
        result.append(pixels)
    return result


def templates(master_edge_map, spatial_meta, plot_id=None):
    """Generates a tightly cropped binary raster template representing the outline of the 
    projected cadastral plot geometry, along with its bounding offsets.
    """
    canvas = np.zeros_like(master_edge_map, dtype=np.uint8)

    for pixels in _geom_to_pixels(spatial_meta["geom_proj"], spatial_meta["patch_minx"], spatial_meta["patch_maxy"], spatial_meta["img_res"]):
        cv2.polylines(canvas, [pixels], isClosed=True, color=255, thickness=2)

    y_idx, x_idx = np.where(canvas > 0)
    if len(x_idx) == 0:
        raise ValueError(f"Template generation failed for plot {plot_id or 'Unknown'}. Canvas returned blank.")

    x_min, x_max = x_idx.min(), x_idx.max()
    y_min, y_max = y_idx.min(), y_idx.max()

    return canvas[y_min:y_max+1, x_min:x_max+1], x_min, y_min


def calculate_alignment_shift(master_edge_map, tight_template, x_min, y_min, spatial_meta, plot_id=None):
    """Calculates the best-fit translation shift (in both pixels and meters) between the target edge map 
    and the tight template using cross-correlation template matching, and computes a confidence score.
    """
    img_res = spatial_meta["img_res"]

    heatmap = cv2.matchTemplate(master_edge_map, tight_template, cv2.TM_CCOEFF_NORMED)

    if np.any(np.isnan(heatmap)) or np.any(np.isinf(heatmap)):
        heatmap = np.nan_to_num(heatmap, nan=0.0, posinf=0.0, neginf=0.0)

    _, max_val, _, max_loc = cv2.minMaxLoc(heatmap)

    dx_pixels = max_loc[0] - x_min
    dy_pixels = max_loc[1] - y_min
    dx_meters = dx_pixels * img_res[0]
    dy_meters = -(dy_pixels * img_res[1])

    peak_prominence = (max_val - float(np.mean(heatmap))) / (float(np.std(heatmap)) + 1e-6)
    calibrated_confidence = round(float(np.clip(peak_prominence / 5.0, 0.0, 1.0)), 4)

    if plot_id is not None:
        print(f"\n{'='*50}")
        print(f"  MATCH METRICS FOR PLOT ID {plot_id}")
        print(f"{'='*50}")
        print(f"Raw Heatmap Match Value:   {max_val:.4f}")
        print(f"Peak Prominence (raw):     {peak_prominence:.4f}")
        print(f"Calibrated Confidence:     {calibrated_confidence:.4f}")
        print(f"Shift Vector:              dx={dx_pixels}px  dy={dy_pixels}px")
        print(f"Geographic Offset:         East={dx_meters:.3f}m  North={dy_meters:.3f}m")
        print(f"{'='*50}")

    return dx_pixels, dy_pixels, dx_meters, dy_meters, calibrated_confidence, heatmap


def save_validation_overlay(satellite_bg, spatial_meta, dx_pixels, dy_pixels, plot_id, village):
    """Generates and writes a color-coded PNG verification image displaying the original 
    drifted geometry (red), the corrected alignment prediction (green), and the ground truth geometry (blue).
    """
    pn = spatial_meta["pn"]

    if village.example_truths is None or pn not in village.example_truths.index:
        return None

    print(f"Generating validation overlay for Plot ID {plot_id}...")

    bg = satellite_bg.copy()
    patch_minx = spatial_meta["patch_minx"]
    patch_maxy = spatial_meta["patch_maxy"]
    img_res = spatial_meta["img_res"]
    img_crs = spatial_meta["img_crs"]

    for pixels in _geom_to_pixels(spatial_meta["geom_proj"], patch_minx, patch_maxy, img_res):
        cv2.polylines(bg, [pixels], isClosed=True, color=(255, 0, 0), thickness=2)
        cv2.polylines(bg, [pixels + np.array([dx_pixels, dy_pixels])], isClosed=True, color=(0, 255, 0), thickness=2)

    truth_proj = gpd.GeoSeries([village.example_truths.loc[pn].geometry], crs="EPSG:4326").to_crs(img_crs).iloc[0]
    for pixels in _geom_to_pixels(truth_proj, patch_minx, patch_maxy, img_res):
        cv2.polylines(bg, [pixels], isClosed=True, color=(0, 0, 255), thickness=2)

    verification_path = f"test_alignment_verification_{plot_id}.png"
    Image.fromarray(bg).save(verification_path)

    print(f"\n{'='*65}")
    print(f" VALIDATION SCORECARD GENERATED: {verification_path}")
    print(f"{'='*65}")
    print(" 🔴 RED   = Original drifted input")
    print(" 🟢 GREEN = Aligned prediction")
    print(" 🔵 BLUE  = Ground truth")
    print(f"{'='*65}\n")

    return verification_path