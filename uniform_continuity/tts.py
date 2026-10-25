"""用 sherpa-onnx + Kokoro v1.1 中文模型离线合成配音，输出 audio/<场景>_<段>.wav 与 audio/durations.json

准备：
  pip install sherpa-onnx soundfile
  curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_1.tar.bz2
  tar xjf kokoro-multi-lang-v1_1.tar.bz2
用法：python3 tts.py --model-dir kokoro-multi-lang-v1_1 [--sid 3] [--speed 1.0]
"""

import argparse
import json
import os

import numpy as np
import sherpa_onnx
import soundfile as sf

from narration import NARRATION

HERE = os.path.dirname(os.path.abspath(__file__))


def load_tts(d, threads):
    j = lambda name: os.path.join(d, name)
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=j("model.onnx"), voices=j("voices.bin"), tokens=j("tokens.txt"),
                data_dir=j("espeak-ng-data"), dict_dir=j("dict"),
                lexicon=j("lexicon-us-en.txt") + "," + j("lexicon-zh.txt")),
            num_threads=threads),
        rule_fsts=",".join(j(f) for f in ("phone-zh.fst", "date-zh.fst", "number-zh.fst")))
    return sherpa_onnx.OfflineTts(cfg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--sid", type=int, default=3, help="说话人编号")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--threads", type=int, default=os.cpu_count())
    a = ap.parse_args()

    out = os.path.join(HERE, "audio")
    os.makedirs(out, exist_ok=True)
    tts = load_tts(a.model_dir, a.threads)
    durations = {}
    for scene, lines in NARRATION.items():
        durations[scene] = []
        for i, text in enumerate(lines):
            audio = tts.generate(text, sid=a.sid, speed=a.speed)
            s = np.asarray(audio.samples, dtype=np.float32)
            # 去掉首尾静音，便于精确对齐
            nz = np.flatnonzero(np.abs(s) > 0.01)
            if len(nz):
                s = s[max(0, nz[0] - 1200):nz[-1] + 2400]
            sf.write(os.path.join(out, f"{scene}_{i}.wav"), s, audio.sample_rate)
            durations[scene].append(len(s) / audio.sample_rate)
            print(f"{scene}[{i}] {durations[scene][-1]:.2f}s", flush=True)
    with open(os.path.join(out, "durations.json"), "w") as fh:
        json.dump(durations, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
