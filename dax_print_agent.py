#!/usr/bin/env python3
"""DAX · Agente de impresión.

Programa pequeño que corre en un equipo del local (Windows, Mac o Linux). Consulta al POS qué hay que imprimir, lo manda a la impresora (por red,
USB/cola del sistema o puerto serie), confirma cada trabajo y avisa cada 15 segundos que sigue vivo y cómo está cada impresora.

Sin dependencias: solo la biblioteca estándar de Python 3.8+. Primera vez:    python dax_print_agent.py --setup
Después, para dejarlo corriendo:                                               python dax_print_agent.py
(En Windows se puede empaquetar como un solo .exe: ver README.md.)

Qué NO hace: no guarda los documentos (los imprime y los olvida), no abre ningún puerto y solo habla con el POS por HTTPS, con su propia clave.
"""
import argparse
import base64
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.2.0"
log = logging.getLogger("dax-print-agent")


# ------------------------------------------------------------------- configuración ---
# Carpeta propia cuando corre como servicio del equipo (instalador de Windows: C:\ProgramData\DAX, protegida para SYSTEM y
# Administradores). Sin ella, la carpeta del usuario, como siempre. Se fija con --home o la variable DAX_AGENT_HOME.
HOME_OVERRIDE = os.environ.get("DAX_AGENT_HOME") or None


def config_path() -> str:
    if HOME_OVERRIDE:
        return os.path.join(HOME_OVERRIDE, "print-agent.json")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DAX", "print-agent.json")
    return os.path.join(os.path.expanduser("~"), ".config", "dax-print-agent.json")


def log_path() -> str:
    if HOME_OVERRIDE:
        return os.path.join(HOME_OVERRIDE, "print-agent.log")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DAX", "print-agent.log")
    return os.path.join(os.path.expanduser("~"), ".config", "dax-print-agent.log")


def setup_logging() -> None:
    """Consola (si hay) y un archivo con rotación (1 MB × 3): sirve para ver por qué dejó de imprimir cuando corre en segundo plano."""
    handlers = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    try:
        os.makedirs(os.path.dirname(log_path()), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers or None, force=True)


def load_config(path=None) -> dict:
    path = path or config_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_config(config: dict, path=None) -> None:
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    try:
        os.chmod(path, 0o600)          # la clave del equipo no la lee otro usuario
    except OSError:
        pass


# --------------------------------------------------------------------------- HTTP ---
class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


class Api:
    """Cliente mínimo del POS. `request(method, path, body)` devuelve el JSON de la respuesta o lanza ApiError."""

    def __init__(self, server: str, token: str = None, timeout: float = 20.0):
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "User-Agent": f"dax-print-agent/{VERSION}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.server}/api/v1{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:  # noqa: BLE001
                detail = exc.reason
            raise ApiError(exc.code, detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise ApiError(0, f"Sin conexión con el POS ({exc})")


# ---------------------------------------------------------------------- transportes ---
def _split_host(address: str, default_port: int = 9100):
    host, _, port = (address or "").partition(":")
    return host, int(port) if port.isdigit() else default_port


def send_network(address: str, data: bytes, timeout: float = 5.0) -> None:
    host, port = _split_host(address)
    with socket.create_connection((host, port), timeout=timeout) as conn:
        conn.sendall(data)
        time.sleep(0.2)                # deja vaciar el búfer antes de cerrar


def check_network(address: str, timeout: float = 2.0):
    host, port = _split_host(address)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "online", None
    except OSError as exc:
        return "offline", f"No responde en {host}:{port} ({exc.__class__.__name__})"


def _win_raw_print(printer_name: str, data: bytes) -> None:
    """Envía bytes CRUDOS a una impresora instalada en Windows (la cola del sistema), sin pasar por el controlador gráfico."""
    import ctypes
    from ctypes import wintypes

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]

    winspool = ctypes.WinDLL("winspool.drv")
    handle = wintypes.HANDLE()
    if not winspool.OpenPrinterW(printer_name, ctypes.byref(handle), None):
        raise OSError(f"Windows no encuentra la impresora «{printer_name}». Revisa el nombre en Configuración → Impresoras.")
    try:
        if not winspool.StartDocPrinterW(handle, 1, ctypes.byref(DOC_INFO_1("DAX", None, "RAW"))):
            raise OSError("No se pudo iniciar el documento en la cola de Windows.")
        try:
            winspool.StartPagePrinter(handle)
            written = wintypes.DWORD()
            if not winspool.WritePrinter(handle, data, len(data), ctypes.byref(written)):
                raise OSError("Windows no pudo enviar los datos a la impresora.")
            winspool.EndPagePrinter(handle)
        finally:
            winspool.EndDocPrinter(handle)
    finally:
        winspool.ClosePrinter(handle)


def send_usb(address: str, data: bytes) -> None:
    """USB: en Windows la cola del sistema por nombre; en Linux/Mac una ruta de dispositivo (/dev/usb/lp0) o `lp -o raw`."""
    if os.name == "nt":
        _win_raw_print(address, data)
    elif address.startswith("/"):
        with open(address, "wb") as device:
            device.write(data)
    else:
        subprocess.run(["lp", "-d", address, "-o", "raw"], input=data, check=True, timeout=30)


def check_usb(address: str):
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            winspool = ctypes.WinDLL("winspool.drv")
            handle = wintypes.HANDLE()
            if winspool.OpenPrinterW(address, ctypes.byref(handle), None):
                winspool.ClosePrinter(handle)
                return "online", None
            return "offline", f"Windows no encuentra la impresora «{address}»."
        except OSError as exc:
            return "offline", str(exc)
    if address.startswith("/"):
        return ("online", None) if os.path.exists(address) else ("offline", f"No existe {address}.")
    return "online", None


def send_serial(address: str, data: bytes) -> None:
    path = rf"\\.\{address}" if os.name == "nt" and not address.startswith("\\\\") else address
    with open(path, "wb", buffering=0) as port:
        port.write(data)


def check_serial(address: str):
    if os.name == "nt":
        return "online", None             # en Windows el puerto COM no se puede comprobar sin abrirlo
    return ("online", None) if os.path.exists(address) else ("offline", f"No existe {address}.")


def send_text_spool(address: str, data: bytes) -> None:
    """Texto por la cola del sistema (impresoras con controlador): archivo temporal + orden de imprimir del sistema."""
    with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        if os.name == "nt":
            os.startfile(path, "printto", address)          # noqa: S606 - verbo de impresión de Windows, con el nombre de la impresora
            time.sleep(4)                                    # da tiempo a que el sistema lea el archivo antes de borrarlo
        else:
            subprocess.run(["lp", "-d", address, path], check=True, timeout=30)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def send_to_printer(printer: dict, data: bytes, content_type: str) -> None:
    kind = printer.get("connection_type")
    address = printer.get("address") or ""
    if not address:
        raise OSError(f"La impresora «{printer.get('name')}» no tiene dirección configurada.")
    if content_type == "text/plain":
        send_text_spool(address, data)
    elif kind == "Network":
        send_network(address, data)
    elif kind == "Bluetooth":
        send_serial(address, data)
    else:
        send_usb(address, data)


def check_printer(printer: dict):
    kind, address = printer.get("connection_type"), printer.get("address") or ""
    if not address:
        return "offline", "Sin dirección configurada."
    if kind == "Network":
        return check_network(address)
    if kind == "Bluetooth":
        return check_serial(address)
    return check_usb(address)


# ------------------------------------------------------------------------- agente ---
class Agent:
    def __init__(self, api, printer_ids=None, sender=send_to_printer, checker=check_printer, sleep=time.sleep):
        self.api = api
        self.printer_ids = printer_ids            # None = atiende todas las impresoras del restaurante
        self.sender, self.checker, self.sleep = sender, checker, sleep
        self.printers = {}
        self.poll_seconds, self.heartbeat_seconds = 2.0, 15.0
        self._last_heartbeat = 0.0
        self.stopped = False

    def hello(self):
        info = self.api.request("POST", "/agent/hello", {})
        self.poll_seconds = float(info.get("poll_seconds", 2))
        self.heartbeat_seconds = float(info.get("heartbeat_seconds", 15))
        self.printers = {p["id"]: p for p in info.get("printers", []) if self.printer_ids is None or p["id"] in self.printer_ids}
        return info

    def heartbeat(self):
        reports = []
        for printer in self.printers.values():
            status, detail = self.checker(printer)
            reports.append({"id": printer["id"], "status": status, "detail": detail})
        self.api.request("POST", "/agent/heartbeat", {"version": VERSION, "hostname": socket.gethostname(), "os": sys.platform, "printers": reports})
        self._last_heartbeat = time.time()

    def process_jobs(self) -> int:
        """Pide trabajos, los imprime y confirma cada uno. Devuelve cuántos tomó."""
        ids = ",".join(str(i) for i in self.printers) if self.printer_ids is not None else ""
        query = f"?limit=5{'&printer_ids=' + ids if ids else ''}"
        jobs = self.api.request("GET", f"/agent/jobs{query}").get("jobs", [])
        for job in jobs:
            printer = self.printers.get(job["printer_id"])
            try:
                if printer is None:
                    raise OSError("Esta impresora no está en la configuración de este equipo.")
                self.sender(printer, base64.b64decode(job["data"]), job["content_type"])
                result = {"ok": True}
                log.info("Impreso: %s", job.get("title"))
            except Exception as exc:  # noqa: BLE001 - cualquier fallo de la impresora se informa al POS, que reintenta
                result = {"ok": False, "error": str(exc)[:300]}
                log.warning("No se pudo imprimir %s: %s", job.get("title"), exc)
            self.api.request("POST", f"/agent/jobs/{job['id']}/result", result)
        return len(jobs)

    def tick(self):
        if time.time() - self._last_heartbeat >= self.heartbeat_seconds:
            self.hello()                                     # recoge cambios de configuración (impresoras nuevas o editadas)
            self.heartbeat()
        return self.process_jobs()

    def run(self):
        backoff = 2.0
        while not self.stopped:
            try:
                self.tick()
                backoff = 2.0
                self.sleep(self.poll_seconds)
            except ApiError as exc:
                if exc.status in (401, 403):
                    log.error("El POS rechazó la clave de este equipo (%s). Genera un código nuevo y ejecuta --setup.", exc.detail)
                    return
                log.warning("Sin conexión con el POS (%s). Reintento en %.0f s.", exc.detail, backoff)
                self.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


# ------------------------------------------------------------ inicio automático ---
TASK_NAME = "DAX Print Agent"
LAUNCHD_LABEL = "shop.dax.print-agent"
SYSTEMD_UNIT = "dax-print-agent.service"


def launch_command(frozen=None, executable=None, script=None) -> list:
    """Cómo se vuelve a abrir este mismo programa: el .exe empaquetado, o Python (sin ventana de consola en Windows) con este archivo."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = executable or sys.executable
    if frozen:
        return [executable]
    if os.name == "nt":
        pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")
        executable = pythonw if os.path.exists(pythonw) else executable
    return [executable, os.path.abspath(script or __file__)]


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def windows_task_script(command: list, workdir: str) -> str:
    """Tarea programada de Windows: se abre al iniciar sesión, aunque el equipo funcione con batería, se vuelve a abrir sola si se cae (cada minuto) y
    nunca se detiene por tiempo. No necesita permisos de administrador."""
    return textwrap.dedent(f"""\
        $ErrorActionPreference = 'Stop'
        $user = "$env:USERDOMAIN\\$env:USERNAME"
        $action = New-ScheduledTaskAction -Execute {ps_quote(command[0])}{(" -Argument " + ps_quote(subprocess.list2cmdline(command[1:]))) if command[1:] else ""} -WorkingDirectory {ps_quote(workdir)}
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Envía a las impresoras térmicas lo que deja el POS DAX' -Force | Out-Null
        Start-ScheduledTask -TaskName {ps_quote(TASK_NAME)}
        """)


# SIDs (no nombres: en un Windows en español «Administrators» se llama «Administradores»): SYSTEM y el grupo Administradores.
SYSTEM_SID, ADMINS_SID = "*S-1-5-18", "*S-1-5-32-544"


def secure_home_command(home: str) -> list:
    """icacls: la carpeta (con la clave del equipo) solo la leen SYSTEM y los Administradores; se quita la herencia de «Usuarios»."""
    return ["icacls", home, "/inheritance:r", "/grant:r", f"{SYSTEM_SID}:(OI)(CI)F", f"{ADMINS_SID}:(OI)(CI)F"]


def secure_home(home: str, runner=None) -> None:
    os.makedirs(home, exist_ok=True)
    if os.name == "nt":
        result = (runner or _run)(secure_home_command(home))
        if result.returncode != 0:
            raise OSError("No se pudo proteger la carpeta del agente: " + (result.stderr or result.stdout).strip())


def windows_system_task_script(command: list, workdir: str) -> str:
    """Tarea programada del EQUIPO (instalador): arranca al encender Windows aunque nadie inicie sesión (cortes de luz,
    actualizaciones), corre como SYSTEM, se vuelve a abrir sola si se cae (cada minuto) y nunca se detiene por tiempo.
    Reemplaza la tarea de usuario con el mismo nombre si existía."""
    return textwrap.dedent(f"""\
        $ErrorActionPreference = 'Stop'
        $action = New-ScheduledTaskAction -Execute {ps_quote(command[0])}{(" -Argument " + ps_quote(subprocess.list2cmdline(command[1:]))) if command[1:] else ""} -WorkingDirectory {ps_quote(workdir)}
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'DAX · Envía a las impresoras térmicas lo que deja el POS (arranca con el equipo)' -Force | Out-Null
        Start-ScheduledTask -TaskName {ps_quote(TASK_NAME)}
        """)


def install_system(dry_run=False, runner=None) -> int:
    """Instalador de Windows: deja el agente arrancando con el equipo como SYSTEM, con la configuración en --home."""
    runner = runner or _run
    if os.name != "nt" and not dry_run:
        print("--install-system es solo para Windows (lo usa el instalador). En Mac/Linux usa --install.")
        return 1
    if not HOME_OVERRIDE:
        print("--install-system necesita --home (la carpeta del equipo, p. ej. C:\\ProgramData\\DAX).")
        return 1
    if not load_config().get("token") and not dry_run:
        print("Primero conecta este equipo con el POS (--setup).")
        return 1
    command = launch_command() + ["--home", HOME_OVERRIDE]
    workdir = os.path.dirname(command[0]) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    script = windows_system_task_script(command, workdir)
    if dry_run:
        print(script)
        return 0
    result = runner(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])
    if result.returncode != 0:
        print("No se pudo crear la tarea del equipo:\n" + (result.stderr or result.stdout).strip())
        return 1
    print(f"Listo: «{TASK_NAME}» arranca sola con el equipo (sin iniciar sesión) y ya quedó funcionando. Registro: {log_path()}")
    return 0


def systemd_unit(command: list) -> str:
    return textwrap.dedent(f"""\
        [Unit]
        Description=DAX · Agente de impresión
        After=network-online.target
        Wants=network-online.target

        [Service]
        ExecStart={" ".join(_shell_quote(c) for c in command)}
        Restart=always
        RestartSec=10

        [Install]
        WantedBy=default.target
        """)


def _shell_quote(value: str) -> str:
    return value if value and all(c.isalnum() or c in "/._-=:" for c in value) else "'" + value.replace("'", "'\\''") + "'"


def launchd_plist(command: list, log_file: str) -> str:
    from xml.sax.saxutils import escape
    args = "".join(f"        <string>{escape(c)}</string>\n" for c in command)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n'
            f'    <key>Label</key><string>{LAUNCHD_LABEL}</string>\n    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n'
            f'    <key>RunAtLoad</key><true/>\n    <key>KeepAlive</key><true/>\n'
            f'    <key>StandardOutPath</key><string>{escape(log_file)}</string>\n    <key>StandardErrorPath</key><string>{escape(log_file)}</string>\n</dict>\n</plist>\n')


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def install_autostart(dry_run=False, runner=_run) -> int:
    """Deja el agente configurado para abrirse solo cada vez que se inicia sesión en el equipo (y volver a abrirse si se cae)."""
    if not load_config().get("token") and not dry_run:
        print("Primero conecta este equipo con el POS:  python dax_print_agent.py --setup")
        return 1
    command = launch_command()
    workdir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, "frozen", False) else os.path.dirname(sys.executable)
    if os.name == "nt":
        script = windows_task_script(command, workdir)
        if dry_run:
            print(script)
            return 0
        result = runner(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])
        if result.returncode != 0:
            print("No se pudo crear la tarea programada:\n" + (result.stderr or result.stdout).strip())
            return 1
        print(f"Listo: «{TASK_NAME}» se abrirá sola cada vez que inicies sesión en Windows y ya quedó funcionando.\n"
              "Si el equipo se reinicia solo (cortes de luz, actualizaciones), activa también el inicio de sesión automático de Windows (netplwiz) para que no espere a que alguien entre.\n"
              f"Registro: {log_path()}")
        return 0
    if sys.platform == "darwin":
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
        text = launchd_plist(command, log_path())
        if dry_run:
            print(f"# {plist}\n{text}")
            return 0
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        with open(plist, "w", encoding="utf-8") as handle:
            handle.write(text)
        runner(["launchctl", "unload", plist])
        result = runner(["launchctl", "load", "-w", plist])
        print("Listo: el agente se abrirá solo al iniciar sesión." if result.returncode == 0 else "No se pudo cargar el servicio:\n" + (result.stderr or result.stdout).strip())
        return result.returncode
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    unit_file = os.path.join(unit_dir, SYSTEMD_UNIT)
    text = systemd_unit(command)
    if dry_run:
        print(f"# {unit_file}\n{text}")
        return 0
    os.makedirs(unit_dir, exist_ok=True)
    with open(unit_file, "w", encoding="utf-8") as handle:
        handle.write(text)
    runner(["systemctl", "--user", "daemon-reload"])
    result = runner(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
    print("Listo: el agente se abrirá solo. Para que también arranque sin iniciar sesión: sudo loginctl enable-linger $USER" if result.returncode == 0
          else "No se pudo activar el servicio:\n" + (result.stderr or result.stdout).strip())
    return result.returncode


def uninstall_autostart(runner=_run) -> int:
    if os.name == "nt":
        script = f"Stop-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Confirm:$false"
        result = runner(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])
    elif sys.platform == "darwin":
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
        result = runner(["launchctl", "unload", "-w", plist])
        if os.path.exists(plist):
            os.remove(plist)
    else:
        result = runner(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
    print("Listo: el agente ya no se abrirá solo." if result.returncode == 0 else "No se pudo quitar:\n" + (result.stderr or result.stdout).strip())
    return result.returncode


def status(runner=_run) -> int:
    """Para saber en un vistazo si está listo: ¿conectado al POS?, ¿configurado para abrirse solo?, ¿dónde está el registro?"""
    config = load_config()
    print(f"DAX · Agente de impresión {VERSION}")
    print(f"Conexión con el POS: {'configurada (' + str(config.get('server')) + ')' if config.get('token') else 'NO configurada (ejecuta --setup)'}")
    if os.name == "nt":
        result = runner(["powershell", "-NoProfile", "-Command", f"(Get-ScheduledTask -TaskName {ps_quote(TASK_NAME)}).State"])
        print(f"Inicio automático: {'activo (' + result.stdout.strip() + ')' if result.returncode == 0 else 'NO configurado (ejecuta --install)'}")
    elif sys.platform == "darwin":
        print(f"Inicio automático: {'activo' if os.path.exists(os.path.expanduser(f'~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist')) else 'NO configurado (ejecuta --install)'}")
    else:
        result = runner(["systemctl", "--user", "is-enabled", SYSTEMD_UNIT])
        print(f"Inicio automático: {'activo' if result.returncode == 0 else 'NO configurado (ejecuta --install)'}")
    print(f"Registro de lo que hace: {log_path()}")
    return 0


# ------------------------------------------------------------------------ comandos ---
def write_setup_result(ok: bool, message: str) -> None:
    """El instalador corre --setup sin ventana: deja el resultado en un archivo para mostrarle a la persona por qué falló."""
    if not HOME_OVERRIDE:
        return
    try:
        with open(os.path.join(HOME_OVERRIDE, "setup-result.txt"), "w", encoding="utf-8-sig") as handle:   # con BOM: el instalador lo lee bien
            handle.write(("OK: " if ok else "ERROR: ") + str(message))
    except OSError:
        pass


def setup(args) -> int:
    print("DAX · Conectar este equipo a tu POS\n")
    server = args.server or input("Dirección de tu POS (ej. https://mirestaurante.com): ").strip()
    if not server.startswith("http"):
        server = "https://" + server
    code = args.code or input("Código de emparejamiento (lo genera el administrador en Configuración → Impresión): ").strip()
    if HOME_OVERRIDE:
        secure_home(HOME_OVERRIDE)                         # antes de guardar la clave del equipo
    try:
        result = Api(server).request("POST", "/agent/pair", {"code": code, "hostname": socket.gethostname()})
    except ApiError as exc:
        print(f"\nNo se pudo conectar: {exc.detail}")
        write_setup_result(False, exc.detail)
        return 1
    save_config({"server": server, "token": result["token"], "restaurant": result.get("restaurant")})
    write_setup_result(True, f"Conectado a «{result.get('restaurant')}».")
    print(f"\nListo: este equipo quedó conectado a «{result.get('restaurant')}». Ahora ejecuta el programa sin opciones para empezar a imprimir.")
    print(f"La configuración quedó en {config_path()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DAX · Agente de impresión")
    parser.add_argument("--setup", action="store_true", help="conectar este equipo con un código de emparejamiento")
    parser.add_argument("--server", help="dirección del POS (con --setup)")
    parser.add_argument("--code", help="código de emparejamiento (con --setup)")
    parser.add_argument("--printers", help="ids de las impresoras que atiende este equipo, separados por coma (por defecto, todas)")
    parser.add_argument("--once", action="store_true", help="hace una sola ronda y termina (para probar)")
    parser.add_argument("--install", action="store_true", help="dejar el agente configurado para abrirse solo al iniciar sesión (y volver a abrirse si se cae)")
    parser.add_argument("--uninstall", action="store_true", help="quitar el inicio automático")
    parser.add_argument("--status", action="store_true", help="ver si está conectado y si abre solo")
    parser.add_argument("--dry-run", action="store_true", help="con --install: muestra lo que haría sin hacerlo")
    parser.add_argument("--home", help="carpeta de configuración y registro del equipo (la usa el instalador de Windows)")
    parser.add_argument("--install-system", action="store_true", help="Windows, como administrador: arrancar con el equipo como SYSTEM (requiere --home)")
    parser.add_argument("--pause", action="store_true", help="con --status: esperar Enter antes de cerrar (acceso directo)")
    args = parser.parse_args(argv)
    global HOME_OVERRIDE
    if args.home:
        HOME_OVERRIDE = os.path.abspath(args.home)
    setup_logging()
    if args.install_system:
        return install_system(dry_run=args.dry_run)
    if args.setup:
        return setup(args)
    if args.install:
        return install_autostart(dry_run=args.dry_run)
    if args.uninstall:
        return uninstall_autostart()
    if args.status:
        code = status()
        if args.pause:
            input("\nPresiona Enter para cerrar…")
        return code
    config = load_config()
    if not config.get("token"):
        print("Este equipo todavía no está conectado. Ejecuta:  python dax_print_agent.py --setup")
        return 1
    ids = [int(x) for x in args.printers.split(",") if x.strip().isdigit()] if args.printers else config.get("printer_ids")
    agent = Agent(Api(config["server"], config["token"]), ids)
    try:
        info = agent.hello()
    except ApiError as exc:
        print(f"No se pudo iniciar: {exc.detail}")
        return 1
    log.info("Conectado a «%s». Impresoras: %s", info.get("restaurant"), ", ".join(p["name"] for p in agent.printers.values()) or "ninguna configurada todavía")
    if args.once:
        agent.heartbeat()
        agent.process_jobs()
        return 0
    try:
        agent.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
