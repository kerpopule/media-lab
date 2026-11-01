"""Patch pinned MuseTalk checkpoint loaders for PyTorch >=2.6.

PyTorch changed torch.load(weights_only) from False to True. MuseTalk's official
legacy checkpoints require explicit False. Inference is network-isolated and all
checkpoint hashes are recorded before this compatibility patch is used.
"""
from pathlib import Path

ROOT = Path("/opt/MuseTalk")
REPLACEMENTS = {
    "musetalk/whisper/whisper/__init__.py": [
        ("torch.load(fp, map_location=device)", "torch.load(fp, map_location=device, weights_only=False)"),
    ],
    "musetalk/utils/face_parsing/resnet.py": [
        ("torch.load(model_path)", "torch.load(model_path, weights_only=False)"),
    ],
    "musetalk/utils/face_parsing/__init__.py": [
        ("torch.load(model_pth)", "torch.load(model_pth, weights_only=False)"),
        ("torch.load(model_pth, map_location=torch.device('cpu'))", "torch.load(model_pth, map_location=torch.device('cpu'), weights_only=False)"),
    ],
    "musetalk/utils/face_detection/detection/sfd/sfd_detector.py": [
        ("torch.load(path_to_detector)", "torch.load(path_to_detector, weights_only=False)"),
    ],
    "musetalk/models/unet.py": [
        ("torch.load(model_path) if torch.cuda.is_available() else torch.load(model_path, map_location=self.device)",
         "torch.load(model_path, weights_only=False) if torch.cuda.is_available() else torch.load(model_path, map_location=self.device, weights_only=False)"),
    ],
}
for relative, pairs in REPLACEMENTS.items():
    path = ROOT / relative
    text = path.read_text()
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"expected loader not found: {relative}: {old}")
        text = text.replace(old, new)
    path.write_text(text)
print("patched", len(REPLACEMENTS), "MuseTalk checkpoint loader files")
