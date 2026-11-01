"""Queue the CPU web origin after the old GPU app is stopped for a pair reservation."""
from pathlib import Path
import subprocess
marker=Path.home()/'.local/state/spark-pair-control/reservation.json'
if marker.is_file():
    subprocess.run(['/usr/bin/systemctl','--user','--no-block','start','media-lab-web.service'],check=True,timeout=15)
