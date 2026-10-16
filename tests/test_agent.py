"""Pruebas del agente que no necesitan el POS ni Windows: modo de equipo (--home), tarea al encender, carpeta protegida y
resultado de la conexión para el instalador."""
import importlib.util
import json
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "dax_print_agent.py"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("dax_print_agent", AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HOME_OVERRIDE", str(tmp_path / "DAX"))
    return module


def test_home_holds_config_and_log(agent, tmp_path):
    assert agent.config_path() == str(tmp_path / "DAX" / "print-agent.json")
    assert agent.log_path() == str(tmp_path / "DAX" / "print-agent.log")


def test_folder_is_locked_to_system_and_administrators_by_sid(agent):
    command = agent.secure_home_command(r"C:\ProgramData\DAX")
    assert command[:3] == ["icacls", r"C:\ProgramData\DAX", "/inheritance:r"]
    assert "*S-1-5-18:(OI)(CI)F" in command and "*S-1-5-32-544:(OI)(CI)F" in command    # sirve en Windows en español
    assert not any("Users" in part or "Usuarios" in part for part in command)


def test_system_task_starts_with_the_computer_as_system_and_restarts(agent):
    script = agent.windows_system_task_script([r"C:\Program Files\DAX Impresion\DAXPrintAgent.exe", "--home", r"C:\ProgramData\DAX"],
                                              r"C:\Program Files\DAX Impresion")
    assert "New-ScheduledTaskTrigger -AtStartup" in script and "-AtLogOn" not in script
    assert "-UserId 'SYSTEM' -LogonType ServiceAccount" in script
    assert "-RestartCount 999" in script and "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "'--home \"C:\\ProgramData\\DAX\"'" in script or "'--home C:\\ProgramData\\DAX'" in script
    assert "-Execute 'C:\\Program Files\\DAX Impresion\\DAXPrintAgent.exe'" in script


def test_install_system_requires_home_and_a_connected_computer(agent, monkeypatch, capsys):
    monkeypatch.setattr(agent, "HOME_OVERRIDE", None)
    assert agent.install_system(dry_run=True) == 1
    monkeypatch.setattr(agent.os, "name", "nt")
    monkeypatch.setattr(agent, "HOME_OVERRIDE", "C:/ProgramData/DAX")
    assert agent.install_system(runner=lambda cmd: pytest.fail("no debe ejecutar nada sin conexión")) == 1
    assert "Primero conecta" in capsys.readouterr().out


def test_failed_setup_leaves_a_readable_reason_for_the_installer(agent, tmp_path, monkeypatch):
    class FailingApi:
        def __init__(self, server, token=None, timeout=20.0):
            pass

        def request(self, method, path, body=None):
            raise agent.ApiError(400, "El código ya se usó o venció.")

    monkeypatch.setattr(agent, "Api", FailingApi)
    args = agent.argparse.Namespace(server="https://pos.example", code="ABCD1234")
    assert agent.setup(args) == 1
    result = (tmp_path / "DAX" / "setup-result.txt").read_text(encoding="utf-8-sig")
    assert result == "ERROR: El código ya se usó o venció."
    assert not (tmp_path / "DAX" / "print-agent.json").exists()


def test_successful_setup_saves_the_key_only_in_the_computer_folder(agent, tmp_path, monkeypatch):
    class OkApi:
        def __init__(self, server, token=None, timeout=20.0):
            self.server = server

        def request(self, method, path, body=None):
            assert path == "/agent/pair" and body["code"] == "ABCD1234"
            return {"token": "clave-sintetica", "restaurant": "The Burgery"}

    monkeypatch.setattr(agent, "Api", OkApi)
    assert agent.setup(agent.argparse.Namespace(server="pos.example", code="ABCD1234")) == 0
    saved = json.loads((tmp_path / "DAX" / "print-agent.json").read_text(encoding="utf-8"))
    assert saved == {"server": "https://pos.example", "token": "clave-sintetica", "restaurant": "The Burgery"}
    assert (tmp_path / "DAX" / "setup-result.txt").read_text(encoding="utf-8-sig").startswith("OK: Conectado a «The Burgery»")


def test_home_flag_is_made_absolute(agent, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent, "status", lambda: 0)
    assert agent.main(["--home", "carpeta", "--status"]) == 0
    assert agent.HOME_OVERRIDE == str(tmp_path / "carpeta")
