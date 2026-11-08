#!/usr/bin/env python3
"""ASR worker persistant (whisper local).

Protocole : une ligne JSON sur stdin -> une ligne JSON sur stdout.
    {"audio": "/tmp/x.webm", "lang": "zh"}  ->  {"text": "你好"}
Le modèle est chargé une seule fois, ce qui ramène une vérification de
prononciation à ~2-4 s au lieu de ~15 s.
"""
import glob
import json
import os
import sys

import av

# av >= 15 a retiré le paramètre `metadata_errors` que faster-whisper passe encore : shim minimal.
_av_open = av.open


def _open(*args, **kwargs):
    kwargs.pop("metadata_errors", None)
    return _av_open(*args, **kwargs)


av.open = _open

from faster_whisper import WhisperModel  # noqa: E402


def model_source():
    """Le modèle téléchargé une fois est réutilisé depuis le cache local : pas de réseau au démarrage."""
    local = os.path.expanduser(
        "~/.cache/huggingface/hub/models--Systran--faster-whisper-small/snapshots/*/")
    for d in sorted(glob.glob(local)):
        if os.path.exists(os.path.join(d, "model.bin")):
            return d
    return "small"


def main():
    model = WhisperModel(model_source(), device="cpu", compute_type="int8")
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            segments, info = model.transcribe(req["audio"], language=req.get("lang", "zh"),
                                              vad_filter=True, beam_size=1,
                                              condition_on_previous_text=False)
            text = "".join(s.text for s in segments).strip()
            print(json.dumps({"text": text, "lang": info.language}, ensure_ascii=False), flush=True)
        except Exception as e:                      # une erreur ne doit pas tuer le worker
            print(json.dumps({"error": str(e)[:200]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
