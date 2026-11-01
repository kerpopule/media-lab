# Media Lab

**An open-source, self-hosted AI media studio.** Create and manage images, video, music, characters and storyboards using your own hardware and explicitly configured providers.

Media Lab now has its own repository: **[kerpopule/media-lab](https://github.com/kerpopule/media-lab)**. VibeXStudio is a separate project and remains at [kerpopule/vibexstudio](https://github.com/kerpopule/vibexstudio).

## Start here

```sh
git clone https://github.com/kerpopule/media-lab.git
cd media-lab/media-lab
./install.sh
```

Read the [studio README](media-lab/README.md), [installation guide](media-lab/docs/INSTALL.md), [capability status](docs/CAPABILITY-STATUS.md), and [engine licenses](media-lab/docs/ENGINE-LICENCES.md) before choosing engines. The controller is Apache-2.0; model weights and third-party runtimes have their own terms and are not bundled. Provider accounts and charges are separate. Installation can start a local service; review its options for your machine.

## Repository layout

- `media-lab/`: application, web UI, controller, adapters, installer, tests and documentation
- `docs/`: capability inventory
- `.github/workflows/`: identity/privacy guard and the arm64 CPU test suite

The existing `media-lab/` subdirectory is deliberately retained so scripts, source bundles and the tagged deployment road keep their paths. There is no VibeXStudio app or desktop source in this repository.

## HDR + vibrant SDR delivery

One retained engine original can produce two verified downloads without a second AI render. Managed HLG/BT.2020 delivery remains H.264 High **8-bit**; it is not sensor-native HDR or recovered highlight detail. The second delivery is vibrant BT.709 SDR. See the [delivery contract](media-lab/docs/DUAL-DELIVERY.md) and [executed verification and limitations](media-lab/docs/DUAL-DELIVERY-VERIFICATION.md).

## Development

Python 3.11+, ffmpeg and ffprobe are required for the media checks.

```sh
cd media-lab
python -m pip install -r requirements-dev.txt
python -m pytest -q -m 'not spark and not gpu'
cd ..
python media-lab/tools/identity_guard.py
```

Host/GPU-dependent tests require their declared hardware and services. Passing CPU checks does not qualify every model, device or physical HDR display.

## Separation and provenance

This is a source-snapshot extraction from the public VibeXStudio repository plus its reviewed dual-delivery candidate. [EXTRACTION.md](EXTRACTION.md) records exact upstream revisions. Existing upstream history remains available there; it was not rewritten or removed. Private live-installation overlays, credentials, models, generated media and runtime state are not imported. Creating this repository does **not** migrate or restart an existing installation.
