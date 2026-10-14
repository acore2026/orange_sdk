#!/usr/bin/env python3
"""Offline two-hop VP8 comparison. No sockets, camera, robot or model downloads."""
import argparse
import csv
from fractions import Fraction
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sys
import types
import importlib.util
import time

import av
import cv2
import numpy as np
from aiortc.codecs.vpx import Vp8Decoder, Vp8Encoder, vp8_depayload
from aiortc.jitterbuffer import JitterFrame

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python/src'))
# Load the exact codec policy without importing AgentSdk/network transports.
package = types.ModuleType('offline_video_policy')
package.__path__ = [str(Path(__file__).resolve().parents[1] / 'python/src/agent_sdk')]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location('offline_video_policy.rtc_policy', Path(package.__path__[0]) / 'rtc_policy.py')
policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)
_BoundedEncoder = policy._BoundedEncoder


def sources(path, count, fps):
    if path:
        # Normalize by timestamps rather than relabeling e.g. 30 fps as 20 fps.
        with av.open(str(path)) as container:
            first = None
            next_at = 0.0
            for frame in container.decode(video=0):
                if frame.time is None:
                    raise ValueError('Input must have presentation timestamps')
                first = frame.time if first is None else first
                if frame.time - first + 1e-6 < next_at:
                    continue
                yield frame.reformat(width=640, height=480).to_ndarray(format='bgr24')
                next_at += 1 / fps
                count -= 1
                if count == 0:
                    return
        return
    rng = np.random.default_rng(20261014)
    texture = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
    texture = cv2.GaussianBlur(texture, (5, 5), 0)
    for index in range(count):
        # Deterministic standstill / translation / fast pan, not robot footage.
        shift = 0 if index < count // 3 else index * (2 if index < 2 * count // 3 else 8)
        image = np.roll(texture, shift, axis=1).copy()
        cv2.putText(image, f'frame {index:04d}', (25, 55), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)
        yield image


def quantiles(values):
    return dict(mean=float(np.mean(values)), p50=float(np.percentile(values, 50)),
                p95=float(np.percentile(values, 95)), maximum=float(max(values)))


def codec_hop(image, index, fps, encoder, decoder):
    frame = av.VideoFrame.from_ndarray(image, format='bgr24')
    frame.pts, frame.time_base = index * (90000 // fps), Fraction(1, 90000)
    started = time.perf_counter()
    packets, timestamp = encoder.encode(frame, force_keyframe=index % (fps * 2) == 0)
    encode_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    decoded = decoder.decode(JitterFrame(b''.join(vp8_depayload(p) for p in packets), timestamp))
    if len(decoded) != 1:
        raise RuntimeError('Expected exactly one decoded frame')
    result = decoded[0].to_ndarray(format='bgr24')
    return result, encode_ms, (time.perf_counter() - started) * 1000, sum(map(len, packets)), len(packets)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--frames', type=int, default=120)
    p.add_argument('--fps', type=int, choices=(10, 15, 20), default=20)
    p.add_argument('--bitrates', default='250,500,750', help='kbps; test each hop independently and together')
    p.add_argument('--pacing', default='1000000,2000000,3000000', help='bps; modeled budgets, not network measurements')
    p.add_argument('--sandbox-root', type=Path, help='Import current Sandbox bounded encoder; with --model also run its detector')
    p.add_argument('--model', type=Path, help='Existing local model only; no downloads')
    args = p.parse_args()
    rates = [int(v) for v in args.bitrates.split(',')]
    pacing = [int(v) for v in args.pacing.split(',')]
    if args.frames < 1 or any(not 250 <= r <= 1500 for r in rates) or any(r <= 0 for r in pacing):
        p.error('Invalid frame count or bitrate')
    if args.output.exists():
        p.error('Use a new output directory to preserve previous results')
    detector = None
    downstream_encoder = _BoundedEncoder
    if args.sandbox_root:
        sys.path.insert(0, str(args.sandbox_root.resolve()))
        from services.video.rtc_policy import _BoundedEncoder as downstream_encoder
    if args.model:
        if not args.sandbox_root or not args.model.is_file():
            p.error('--model requires an existing local file and --sandbox-root')
        from dataclasses import replace
        from services.video.config import VideoSettings
        from services.video.detector import YoloDetector
        detector = YoloDetector(replace(VideoSettings.from_env(), yolo_model=str(args.model.resolve())))
        detector.warmup()
    images = list(sources(args.input, args.frames, args.fps))
    if not images:
        p.error('No source frames')
    args.output.mkdir(parents=True)
    fingerprint = hashlib.sha256(b''.join(image.tobytes() for image in images)).hexdigest()
    # Include baseline even for a custom list; vary one hop at a time first.
    pairs = sorted({(250,250)} | {(r,250) for r in rates} | {(250,r) for r in rates} | {(r,r) for r in rates})
    summaries = []
    for dog_rate, sandbox_rate in pairs:
        encoders = [_BoundedEncoder(Vp8Encoder(), dog_rate * 1000), downstream_encoder(Vp8Encoder(), sandbox_rate * 1000)]
        for encoder in encoders:
            encoder._encoder.picture_id = 0
        decoders = [Vp8Decoder(), Vp8Decoder()]
        rows = []
        for i, original in enumerate(images):
            incoming, e1, d1, b1, n1 = codec_hop(original, i, args.fps, encoders[0], decoders[0])
            # Compare with the uncompressed input processed identically. For a real
            # detector overlay PSNR is omitted because detections need not match.
            started = time.perf_counter()
            if detector:
                processed, _ = detector.process(incoming)
                reference = None
            else:
                processed = incoming.copy()
                reference = original.copy()
                for image in (processed, reference):
                    cv2.rectangle(image, (220,120), (420,360), (0,255,0), 2)
            processing_ms = (time.perf_counter() - started) * 1000
            final, e2, d2, b2, n2 = codec_hop(processed, i, args.fps, encoders[1], decoders[1])
            mse = float(np.mean((final.astype(np.float32) - reference.astype(np.float32)) ** 2)) if reference is not None else None
            rows.append(dict(frame=i, source_ms=i*1000/args.fps, dog_encode_ms=e1,
                             sandbox_decode_ms=d1, processing_ms=processing_ms, sandbox_encode_ms=e2,
                             receiver_decode_ms=d2, dog_bytes=b1, sandbox_bytes=b2,
                             dog_packets=n1, sandbox_packets=n2,
                             psnr_db=10*math.log10(255**2/max(mse,1e-10)) if mse is not None else None))
            if i in {0, len(images)//2, len(images)-1}:
                cv2.imwrite(str(args.output/f'{dog_rate}-{sandbox_rate}-frame{i}.png'), final)
        with (args.output/f'{dog_rate}-{sandbox_rate}.csv').open('w') as f:
            w = csv.DictWriter(f, fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
        metrics = {key: quantiles([r[key] for r in rows]) for key in rows[0]
                   if key.endswith('_ms') and key != 'source_ms'}
        if not detector:
            metrics['psnr_db'] = quantiles([r['psnr_db'] for r in rows])
        # Explicit budget model: payload + RTP header + pacer's 64-byte allowance.
        budgets = {str(rate): {hop: quantiles([(r[hop+'_bytes'] + 76*r[hop+'_packets'])*8000/rate for r in rows])
                              for hop in ('dog','sandbox')} for rate in pacing}
        summaries.append(dict(dog_kbps=dog_rate, sandbox_kbps=sandbox_rate,
                              metrics=metrics, pacing_budget_ms=budgets,
                              actual_payload_kbps={hop:sum(r[hop+'_bytes'] for r in rows)*8*args.fps/len(rows)/1000
                                                   for hop in ('dog','sandbox')},
                              encoded_frames=len(rows), decoded_frames=len(rows)))
    source_paths = [Path(__file__), Path(package.__path__[0]) / 'rtc_policy.py']
    if args.sandbox_root:
        source_paths.append(args.sandbox_root / 'services/video/rtc_policy.py')
    report = dict(source=str(args.input) if args.input else 'deterministic synthetic texture: static/translation/pan',
                  implementation_sha256={str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
                  source_frames_sha256=fingerprint, frames=len(images), fps=args.fps,
                  codec='VP8', aiortc=importlib.metadata.version('aiortc'), av=av.__version__,
                  processing='real Sandbox detector' if detector else 'synthetic rectangle overlay (no inference)',
                  limitations=['Serial offline codec benchmark, not live 20fps playback or end-to-end latency.',
                               'Pacing budgets are calculated, not MASQUE/socket measurements.',
                               'Receiver is CPU PyAV, not Android EGL; no jitter buffer/network loss modeled.',
                               'All frames encoded: no queue/drop scheduling simulation. See pipeline_metrics for live pipeline counters.',
                               '20fps sampling from lower-fps recordings does not create missing frames.'],
                  cases=summaries)
    (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(args.output/'report.json')


if __name__ == '__main__':
    main()
