import os
import time
from typing import Any, Dict, Iterable, Optional

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
from epics import PV
from scipy.optimize import curve_fit


MOTR_ANALYZER_SELECTED_PV = "mOTR:analyzer:dispersion:selectedmotr"
MOTR_ANALYZER_SIZE_H_PV = "mOTR:analyzer:size:H"
MOTR_ANALYZER_SIZE_V_PV = "mOTR:analyzer:size:V"
MOTR_ANALYZER_CENTER_H_PV = "mOTR:analyzer:center:H"
MOTR_ANALYZER_CENTER_V_PV = "mOTR:analyzer:center:V"
MOTR_IMAGE_SHAPE = (960, 1280)


def gaussian(x, amplitude, mean, stddev, offset):
    """1D Gaussian function for curve fitting."""
    return amplitude * np.exp(-((x - mean) / (2 * stddev)) ** 2) + offset


def _safe_get(pv_name: str, default: Any = np.nan):
    try:
        value = PV(pv_name).get()
    except Exception:
        return default
    if value is None:
        return default
    return value


def _safe_float(value: Any, default: float = np.nan) -> float:
    try:
        if value is None:
            return float(default)
        arr = np.asarray(value)
        if arr.size == 0:
            return float(default)
        return float(arr.flat[0])
    except Exception:
        return float(default)


def _motr_prefix(otr_id: int) -> str:
    return f"mOTR{int(otr_id)}"


def set_selected_motr(otr_id: int, wait_sec: float = 1.0) -> None:
    PV(MOTR_ANALYZER_SELECTED_PV).put(int(otr_id))
    if wait_sec > 0:
        time.sleep(wait_sec)


def get_pixel_calibrations(otr_id: int) -> tuple[float, float]:
    prefix = _motr_prefix(otr_id)
    h_pv_name = f"{prefix}:H:x1:Calibration:Factor"
    v_pv_name = f"{prefix}:V:x1:Calibration:Factor"
    h_factor = _safe_float(_safe_get(h_pv_name), default=np.nan)
    v_factor = _safe_float(_safe_get(v_pv_name), default=np.nan)
    if not np.isfinite(h_factor) or h_factor <= 0:
        h_factor = 1.0
    if not np.isfinite(v_factor) or v_factor <= 0:
        v_factor = 1.0
    return float(h_factor), float(v_factor)


def read_kek_analyzer_snapshot() -> Dict[str, float]:
    return {
        "size_h": _safe_float(_safe_get(MOTR_ANALYZER_SIZE_H_PV)),
        "size_v": _safe_float(_safe_get(MOTR_ANALYZER_SIZE_V_PV)),
        "center_h": _safe_float(_safe_get(MOTR_ANALYZER_CENTER_H_PV)),
        "center_v": _safe_float(_safe_get(MOTR_ANALYZER_CENTER_V_PV)),
    }


def read_kek_analyzer_average(samples: int = 3, interval_sec: float = 1.0) -> Dict[str, Any]:
    size_h_samples = []
    size_v_samples = []
    for sample_idx in range(max(1, int(samples))):
        snapshot = read_kek_analyzer_snapshot()
        size_h_samples.append(float(snapshot["size_h"]))
        size_v_samples.append(float(snapshot["size_v"]))
        if sample_idx < max(1, int(samples)) - 1 and interval_sec > 0:
            time.sleep(interval_sec)

    size_h_arr = np.asarray(size_h_samples, dtype=float)
    size_v_arr = np.asarray(size_v_samples, dtype=float)
    return {
        "size_h_samples": size_h_arr,
        "size_v_samples": size_v_arr,
        "size_h_mean": float(np.nanmean(size_h_arr)) if size_h_arr.size else float("nan"),
        "size_v_mean": float(np.nanmean(size_v_arr)) if size_v_arr.size else float("nan"),
        "size_h_std": float(np.nanstd(size_h_arr, ddof=1)) if size_h_arr.size > 1 else 0.0,
        "size_v_std": float(np.nanstd(size_v_arr, ddof=1)) if size_v_arr.size > 1 else 0.0,
    }


def fit_otr_image(
    img_data: np.ndarray,
    h_factor_um_px: float = 1.0,
    v_factor_um_px: float = 1.0,
) -> Dict[str, Any]:
    clean_img = np.array(img_data, dtype=np.float64, copy=True)
    clean_img[~np.isfinite(clean_img)] = 0.0

    height, width = clean_img.shape
    proj_x = np.sum(clean_img, axis=0)
    proj_y = np.sum(clean_img, axis=1)
    x_coords = np.arange(width)
    y_coords = np.arange(height)

    p0_x = [np.max(proj_x), np.argmax(proj_x), max(width / 10, 1.0), np.min(proj_x)]
    p0_y = [np.max(proj_y), np.argmax(proj_y), max(height / 10, 1.0), np.min(proj_y)]

    sigma_h_um = float("nan")
    sigma_v_um = float("nan")
    sigma_h2_m2 = float("nan")
    sigma_v2_m2 = float("nan")
    fit_x = None
    fit_y = None
    fit_params_x = None
    fit_params_y = None
    center_x_px = width // 2
    center_y_px = height // 2
    radius_x_px = 150
    radius_y_px = 150

    try:
        popt_x, _ = curve_fit(gaussian, x_coords, proj_x, p0=p0_x, maxfev=20000)
        fit_x = gaussian(x_coords, *popt_x)
        fit_params_x = tuple(float(v) for v in popt_x)
        sigma_x_px = abs(float(popt_x[2]))
        sigma_h_um = sigma_x_px * float(h_factor_um_px)
        sigma_h2_m2 = (sigma_h_um * 1e-6) ** 2
        center_x_px = int(round(float(popt_x[1])))
        radius_x_px = max(10, int(round(sigma_x_px * 3.0)))
    except Exception:
        fit_x = None
        fit_params_x = None

    try:
        popt_y, _ = curve_fit(gaussian, y_coords, proj_y, p0=p0_y, maxfev=20000)
        fit_y = gaussian(y_coords, *popt_y)
        fit_params_y = tuple(float(v) for v in popt_y)
        sigma_y_px = abs(float(popt_y[2]))
        sigma_v_um = sigma_y_px * float(v_factor_um_px)
        sigma_v2_m2 = (sigma_v_um * 1e-6) ** 2
        center_y_px = int(round(float(popt_y[1])))
        radius_y_px = max(10, int(round(sigma_y_px * 3.0)))
    except Exception:
        fit_y = None
        fit_params_y = None

    y_min = max(0, center_y_px - radius_y_px)
    y_max = min(height, center_y_px + radius_y_px)
    x_min = max(0, center_x_px - radius_x_px)
    x_max = min(width, center_x_px + radius_x_px)

    roi_img = clean_img[y_min:y_max, x_min:x_max].copy()
    sigma_13_m2 = float("nan")
    threshold_fraction = 0.05
    local_threshold = 0.0

    if roi_img.size > 0:
        local_threshold = float(np.max(roi_img) * threshold_fraction)
        roi_img[roi_img < local_threshold] = 0.0
        total_intensity = float(np.sum(roi_img))
        if total_intensity > 0:
            roi_y_idx, roi_x_idx = np.indices(roi_img.shape)
            x_bar = float(np.sum(roi_x_idx * roi_img) / total_intensity)
            y_bar = float(np.sum(roi_y_idx * roi_img) / total_intensity)
            sig13_px2 = float(
                np.sum((roi_x_idx - x_bar) * (roi_y_idx - y_bar) * roi_img) / total_intensity
            )
            sigma_13_um2 = sig13_px2 * float(h_factor_um_px) * float(v_factor_um_px)
            sigma_13_m2 = sigma_13_um2 * 1e-12

    return {
        "proj_x": proj_x,
        "proj_y": proj_y,
        "sigma_h_um": float(sigma_h_um),
        "sigma_v_um": float(sigma_v_um),
        "sigma_h2_m2": float(sigma_h2_m2),
        "sigma_v2_m2": float(sigma_v2_m2),
        "sigma_13_m2": float(sigma_13_m2),
        "fit_x": fit_x,
        "fit_y": fit_y,
        "fit_params_x": fit_params_x,
        "fit_params_y": fit_params_y,
        "center_x_px": int(center_x_px),
        "center_y_px": int(center_y_px),
        "radius_x_px": int(radius_x_px),
        "radius_y_px": int(radius_y_px),
        "roi_bounds": (int(x_min), int(x_max), int(y_min), int(y_max)),
        "local_threshold": float(local_threshold),
    }


def plot_otr_analysis_inset(
    ax_main,
    img_data: np.ndarray,
    title_str: str,
    h_factor_um_px: float = 1.0,
    v_factor_um_px: float = 1.0,
    analysis: Optional[Dict[str, Any]] = None,
):
    if analysis is None:
        analysis = fit_otr_image(
            img_data,
            h_factor_um_px=h_factor_um_px,
            v_factor_um_px=v_factor_um_px,
        )

    ax_main.imshow(img_data, cmap="gray", origin="lower")

    x_min, x_max, y_min, y_max = analysis["roi_bounds"]
    rect = patches.Rectangle(
        (x_min, y_min),
        x_max - x_min,
        y_max - y_min,
        linewidth=1,
        edgecolor="r",
        facecolor="none",
        alpha=0.5,
    )
    ax_main.add_patch(rect)

    sigma_13_m2 = float(analysis["sigma_13_m2"])
    if np.isfinite(sigma_13_m2):
        ax_main.set_title(f"{title_str} | $\\sigma_{{13}}$: {sigma_13_m2:.2e} m$^2$")
    else:
        ax_main.set_title(title_str)

    proj_x = np.asarray(analysis["proj_x"], dtype=float)
    proj_y = np.asarray(analysis["proj_y"], dtype=float)
    x_coords = np.arange(proj_x.size)
    y_coords = np.arange(proj_y.size)

    ax_hist_x = ax_main.inset_axes([0.0, 1.05, 1.0, 0.2], transform=ax_main.transAxes)
    ax_hist_y = ax_main.inset_axes([1.05, 0.0, 0.2, 1.0], transform=ax_main.transAxes)

    ax_hist_x.plot(x_coords, proj_x, label="H Projection")
    fit_x = analysis.get("fit_x")
    if fit_x is not None and np.isfinite(float(analysis["sigma_h2_m2"])):
        ax_hist_x.plot(x_coords, fit_x, "r--", label=f"Size: {analysis['sigma_h2_m2']:.2e} $m^2$")
    ax_hist_x.legend(fontsize="x-small", loc="upper right")
    ax_hist_x.axis("off")

    ax_hist_y.plot(proj_y, y_coords, label="V Projection")
    fit_y = analysis.get("fit_y")
    if fit_y is not None and np.isfinite(float(analysis["sigma_v2_m2"])):
        ax_hist_y.plot(fit_y, y_coords, "r--", label=f"Size: {analysis['sigma_v2_m2']:.2e} $m^2$")
    ax_hist_y.legend(fontsize="x-small", loc="lower right")
    ax_hist_y.axis("off")

    return (
        proj_x,
        proj_y,
        float(analysis["sigma_h_um"]),
        float(analysis["sigma_v_um"]),
        float(analysis["sigma_13_m2"]),
    )


def _capture_frame(prefix: str, acquire_wait_sec: float) -> np.ndarray:
    PV(f"{prefix}:CAMERA:Acquire").put(1)
    if acquire_wait_sec > 0:
        time.sleep(acquire_wait_sec)
    raw = _safe_get(f"{prefix}:IMAGE:ArrayData", default=None)
    PV(f"{prefix}:CAMERA:Acquire").put(0)
    if raw is None:
        raise RuntimeError(f"Failed to acquire image from {prefix}:IMAGE:ArrayData")
    arr = np.asarray(raw)
    if arr.size != MOTR_IMAGE_SHAPE[0] * MOTR_IMAGE_SHAPE[1]:
        raise RuntimeError(
            f"Unexpected image size for {prefix}: got {arr.size}, "
            f"expected {MOTR_IMAGE_SHAPE[0] * MOTR_IMAGE_SHAPE[1]}"
        )
    return arr.reshape(MOTR_IMAGE_SHAPE).astype(np.float64)


def _median_frames(prefix: str, frame_count: int, acquire_wait_sec: float) -> tuple[np.ndarray, np.ndarray]:
    frames = []
    for _ in range(max(1, int(frame_count))):
        frames.append(_capture_frame(prefix, acquire_wait_sec))
    frames_arr = np.asarray(frames, dtype=np.float64)
    return np.median(frames_arr, axis=0), frames_arr


def acquire_otr_image(
    otr_id: int,
    min_total_intensity: float = 135000,
    max_retries: int = 3,
    background_frames: int = 10,
    beam_frames: int = 5,
    background_acquire_wait_sec: float = 1.0,
    beam_acquire_wait_sec: float = 3.0,
    select_wait_sec: float = 1.0,
    retract_wait_sec: float = 5.0,
    insert_wait_sec: float = 5.0,
    retry_wait_sec: float = 1.0,
    kek_samples: int = 3,
    kek_sample_interval_sec: float = 1.0,
) -> Dict[str, Any]:
    prefix = _motr_prefix(otr_id)
    print(f"\nAcquiring Background and Beam for OTR{otr_id}")
    set_selected_motr(otr_id, wait_sec=select_wait_sec)

    otr_in_pv = PV(f"{prefix}:Target:WRITE:IN")
    otr_out_pv = PV(f"{prefix}:Target:WRITE:OUT")

    print(" -> Retracting screen for background...")
    otr_out_pv.put(1)
    if retract_wait_sec > 0:
        time.sleep(retract_wait_sec)

    bg_img, bg_frames = _median_frames(
        prefix=prefix,
        frame_count=background_frames,
        acquire_wait_sec=background_acquire_wait_sec,
    )

    print(" -> Inserting screen for beam...")
    otr_in_pv.put(1)

    final_beam_img = None
    final_beam_frames = None
    final_subtracted_img = None
    final_total_intensity = float("nan")
    final_kek_average = None

    for attempt in range(1, max(1, int(max_retries)) + 1):
        wait_this_round = insert_wait_sec if attempt == 1 else retry_wait_sec
        if wait_this_round > 0:
            time.sleep(wait_this_round)

        kek_average = read_kek_analyzer_average(
            samples=kek_samples,
            interval_sec=kek_sample_interval_sec,
        )

        beam_img, beam_frames_arr = _median_frames(
            prefix=prefix,
            frame_count=beam_frames,
            acquire_wait_sec=beam_acquire_wait_sec,
        )
        subtracted_img = beam_img - bg_img
        subtracted_img[subtracted_img < 0] = 0
        total_intensity = float(np.sum(subtracted_img))

        final_beam_img = beam_img
        final_beam_frames = beam_frames_arr
        final_subtracted_img = subtracted_img
        final_total_intensity = total_intensity
        final_kek_average = kek_average

        if total_intensity >= float(min_total_intensity):
            break

        print(
            " -> WARNING: Total intensity too low "
            f"({total_intensity:,.0f} < threshold of {float(min_total_intensity):,.0f})."
        )
        if attempt < max(1, int(max_retries)):
            print(" -> Retaking beam image (keeping screen inserted)...")
        else:
            print(" -> Max retries reached. Proceeding with the low-intensity frame.")

    final_snapshot = read_kek_analyzer_snapshot()
    h_factor, v_factor = get_pixel_calibrations(otr_id)

    print(" -> Retracting screen...")
    otr_out_pv.put(1)

    return {
        "otr_id": int(otr_id),
        "prefix": prefix,
        "background_image": bg_img,
        "background_frames": bg_frames,
        "beam_image": final_beam_img,
        "beam_frames": final_beam_frames,
        "subtracted_image": final_subtracted_img,
        "total_intensity": float(final_total_intensity),
        "kek_average": final_kek_average or read_kek_analyzer_average(samples=1, interval_sec=0.0),
        "kek_snapshot": final_snapshot,
        "calibration_h_um_per_px": float(h_factor),
        "calibration_v_um_per_px": float(v_factor),
    }


def measure_single_motr(
    otr_id: int,
    min_total_intensity: float = 135000,
    max_retries: int = 3,
    background_frames: int = 10,
    beam_frames: int = 5,
    background_acquire_wait_sec: float = 1.0,
    beam_acquire_wait_sec: float = 3.0,
    select_wait_sec: float = 1.0,
    retract_wait_sec: float = 5.0,
    insert_wait_sec: float = 5.0,
    retry_wait_sec: float = 1.0,
    kek_samples: int = 3,
    kek_sample_interval_sec: float = 1.0,
) -> Dict[str, Any]:
    raw = acquire_otr_image(
        otr_id=otr_id,
        min_total_intensity=min_total_intensity,
        max_retries=max_retries,
        background_frames=background_frames,
        beam_frames=beam_frames,
        background_acquire_wait_sec=background_acquire_wait_sec,
        beam_acquire_wait_sec=beam_acquire_wait_sec,
        select_wait_sec=select_wait_sec,
        retract_wait_sec=retract_wait_sec,
        insert_wait_sec=insert_wait_sec,
        retry_wait_sec=retry_wait_sec,
        kek_samples=kek_samples,
        kek_sample_interval_sec=kek_sample_interval_sec,
    )

    conrad = fit_otr_image(
        raw["subtracted_image"],
        h_factor_um_px=raw["calibration_h_um_per_px"],
        v_factor_um_px=raw["calibration_v_um_per_px"],
    )
    conrad["image"] = raw["subtracted_image"]

    kek_average = dict(raw["kek_average"])
    kek_average.update(
        {
            "center_h": float(raw["kek_snapshot"]["center_h"]),
            "center_v": float(raw["kek_snapshot"]["center_v"]),
            "calibration_h_um_per_px": float(raw["calibration_h_um_per_px"]),
            "calibration_v_um_per_px": float(raw["calibration_v_um_per_px"]),
        }
    )

    return {
        "otr_id": int(otr_id),
        "prefix": raw["prefix"],
        "background_image": raw["background_image"],
        "background_frames": raw["background_frames"],
        "beam_image": raw["beam_image"],
        "beam_frames": raw["beam_frames"],
        "subtracted_image": raw["subtracted_image"],
        "total_intensity": float(raw["total_intensity"]),
        "conrad": conrad,
        "kek": kek_average,
    }


def measure_motr_set(
    otr_ids: Iterable[int] = (0, 1, 2, 3),
    min_total_intensity: float = 135000,
    max_retries: int = 3,
    background_frames: int = 10,
    beam_frames: int = 5,
    background_acquire_wait_sec: float = 1.0,
    beam_acquire_wait_sec: float = 3.0,
    select_wait_sec: float = 1.0,
    retract_wait_sec: float = 5.0,
    insert_wait_sec: float = 5.0,
    retry_wait_sec: float = 1.0,
    kek_samples: int = 3,
    kek_sample_interval_sec: float = 1.0,
) -> Dict[int, Dict[str, Any]]:
    results: Dict[int, Dict[str, Any]] = {}
    for otr_id in [int(v) for v in otr_ids]:
        results[otr_id] = measure_single_motr(
            otr_id=otr_id,
            min_total_intensity=min_total_intensity,
            max_retries=max_retries,
            background_frames=background_frames,
            beam_frames=beam_frames,
            background_acquire_wait_sec=background_acquire_wait_sec,
            beam_acquire_wait_sec=beam_acquire_wait_sec,
            select_wait_sec=select_wait_sec,
            retract_wait_sec=retract_wait_sec,
            insert_wait_sec=insert_wait_sec,
            retry_wait_sec=retry_wait_sec,
            kek_samples=kek_samples,
            kek_sample_interval_sec=kek_sample_interval_sec,
        )
    return results


def save_measurement_set_npz(save_path: str, results: Dict[int, Dict[str, Any]]) -> None:
    payload: Dict[str, Any] = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    for otr_id, result in sorted(results.items()):
        tag = f"OTR{int(otr_id)}"
        conrad = dict(result["conrad"])
        kek = dict(result["kek"])
        payload[f"{tag}_Image_Raw"] = result["subtracted_image"]
        payload[f"{tag}_Image_BG"] = result["background_image"]
        payload[f"{tag}_Image_Beam"] = result["beam_image"]
        payload[f"{tag}_ProjX"] = conrad["proj_x"]
        payload[f"{tag}_ProjY"] = conrad["proj_y"]
        payload[f"{tag}_SigmaH_um"] = conrad["sigma_h_um"]
        payload[f"{tag}_SigmaV_um"] = conrad["sigma_v_um"]
        payload[f"{tag}_Sigma13_m2"] = conrad["sigma_13_m2"]
        payload[f"{tag}_TotalIntensity"] = result["total_intensity"]
        payload[f"{tag}_KEK_SizeH_Samples"] = kek["size_h_samples"]
        payload[f"{tag}_KEK_SizeV_Samples"] = kek["size_v_samples"]
        payload[f"{tag}_KEK_SizeH_Mean"] = kek["size_h_mean"]
        payload[f"{tag}_KEK_SizeV_Mean"] = kek["size_v_mean"]
        payload[f"{tag}_KEK_SizeH_Std"] = kek["size_h_std"]
        payload[f"{tag}_KEK_SizeV_Std"] = kek["size_v_std"]
        payload[f"{tag}_KEK_CenterH"] = kek["center_h"]
        payload[f"{tag}_KEK_CenterV"] = kek["center_v"]
        payload[f"{tag}_CalibH_um_per_px"] = kek["calibration_h_um_per_px"]
        payload[f"{tag}_CalibV_um_per_px"] = kek["calibration_v_um_per_px"]
    np.savez_compressed(save_path, **payload)


if __name__ == "__main__":
    results = measure_motr_set(
        otr_ids=(0, 1, 2, 3),
        min_total_intensity=130000,
        max_retries=3,
    )

    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(
        nrows=2,
        ncols=2,
        figsize=(8, 6),
        constrained_layout=True,
    )
    axes = [ax1, ax2, ax3, ax4]
    for ax, otr_id in zip(axes, sorted(results.keys())):
        result = results[otr_id]
        plot_otr_analysis_inset(
            ax,
            result["subtracted_image"],
            f"OTR{otr_id}",
            h_factor_um_px=float(result["kek"]["calibration_h_um_per_px"]),
            v_factor_um_px=float(result["kek"]["calibration_v_um_per_px"]),
            analysis=result["conrad"],
        )

    plt.show()

    save_folder = "Data_mOTR"
    file_name = f"OTR_Beam_Data_{time.strftime('%Y%m%d-%H%M%S')}.npz"
    os.makedirs(save_folder, exist_ok=True)
    save_path = os.path.join(save_folder, file_name)
    save_measurement_set_npz(save_path, results)
    print(f"\nSuccessfully saved data to: {save_path}")
