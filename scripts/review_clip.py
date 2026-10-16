"""Render an unmarked multichannel strip from a pilot NPZ for independent review.

Example: python scripts/review_clip.py r_92e5bc2a63448166fbd1_43200 --offset 20
Offset is seconds into the 60s review area (the NPZ also contains 10s buffers).
"""
import argparse
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip_id")
    parser.add_argument("--offset", type=int, default=0, choices=range(0, 51))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "outputs/verification"
    # Accept only a basename within the generated clip directory.
    if Path(args.clip_id).name != args.clip_id or any(c in args.clip_id for c in "/\\:"):
        parser.error("clip_id must be a basename")
    with np.load(root / "clips" / (args.clip_id + ".npz"), allow_pickle=False) as data:
        fs, start = int(data["fs"]), int(data["start_sample"])
        lo, hi = (10 + args.offset) * fs, (20 + args.offset) * fs
        wave = data["raw_counts"][lo:hi]
    fig, axes = plt.subplots(8, 1, figsize=(15, 12), sharex=True)
    for ch, ax in enumerate(axes):
        ax.plot((np.arange(lo, hi) + start) / fs, wave[:, ch], lw=.7)
        ax.set_ylabel("Ch%d\nraw count" % (ch + 1))
        ax.grid(alpha=.2)
    axes[-1].set_xlabel("Seconds from recording start; multiply by 250 for sample index")
    fig.suptitle(args.clip_id + " | UNMARKED waveform; unknown lead mapping/gain\nNo vendor or detector annotations; no diagnostic labels", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, .95))
    out = root / "unmarked"
    out.mkdir(exist_ok=True)
    path = out / (args.clip_id + "_offset%d.png" % args.offset)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(path)


if __name__ == "__main__":
    main()
