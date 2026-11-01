import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "text_runtime_switch", ROOT / "runner" / "text_runtime_switch.py"
)
assert SPEC is not None and SPEC.loader is not None
switch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(switch)


class WaitModelTests(unittest.TestCase):
    def test_flash_model_may_be_advertised_as_llama_cpp_alias(self):
        body = {
            "data": [
                {
                    "id": "media-lab-text",
                    "aliases": ["qwen3.8-flash-next-ud-iq1-m", "media-lab-text"],
                }
            ]
        }
        with mock.patch.object(switch, "http_json", return_value=body):
            self.assertIs(body, switch.wait_model("flash", timeout_s=1))

    def test_pplx_primary_model_id_is_accepted(self):
        body = {
            "data": [
                {"id": "pplx-computer-qwen-3-8-27b-dflash2-20260824"}
            ]
        }
        with mock.patch.object(switch, "http_json", return_value=body):
            self.assertIs(body, switch.wait_model("pplx", timeout_s=1))


if __name__ == "__main__":
    unittest.main()
