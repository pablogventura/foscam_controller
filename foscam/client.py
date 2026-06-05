"""
Cliente HTTP para la API CGI de cámaras Foscam.
Compatible con la mayoría de modelos Foscam (FI98xx, C1, R2, etc.).
"""

import requests
from typing import Any, Dict, Optional
from urllib.parse import urlencode


class FoscamClient:
    """
    Cliente para enviar comandos CGI a una cámara Foscam.
    Soporta comandos con y sin parámetros, y descarga de streams (snapPicture2).
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 88,
        timeout: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout
        self._base_url = f"http://{host}:{port}/cgi-bin/CGIProxy.fcgi"

    @property
    def base_url(self) -> str:
        return self._base_url

    def send(
        self,
        cmd: str,
        params: Optional[Dict[str, Any]] = None,
        stream: bool = False,
    ):
        """
        Envía un comando CGI a la cámara.

        Args:
            cmd: Nombre del comando (ej: getDevInfo, ptzMoveUp, setBrightness).
            params: Parámetros adicionales (ej: {"brightness": 70}).
            stream: Si True, devuelve el objeto response.raw para datos binarios.

        Returns:
            str con la respuesta XML, o el objeto raw si stream=True.
        """
        if params is None:
            params = {}
        full = {"cmd": cmd, "usr": self.user, "pwd": self.password, **params}
        url = f"{self._base_url}?{urlencode(full)}"
        try:
            r = requests.get(url, timeout=self.timeout, stream=stream)
            if stream:
                return r.raw
            return r.text
        except requests.RequestException as e:
            return None

    def ptz_move(self, direction: str) -> Optional[str]:
        """
        Mueve la cámara PTZ. direction: Up, Down, Left, Right.
        Para detener: usar ptz_stop().
        """
        cmd = f"ptzMove{direction}"
        return self.send(cmd)

    def ptz_stop(self) -> Optional[str]:
        """Detiene el movimiento PTZ."""
        return self.send("ptzStopRun")

    def ptz_reset(self) -> Optional[str]:
        """Vuelve a la posición por defecto (preset)."""
        return self.send("ptzReset")

    def ptz_goto_preset(self, name: str) -> Optional[str]:
        """Va al preset con el nombre dado (ej: TopMost, LeftMost)."""
        return self.send("ptzGotoPresetPoint", {"name": name})

    def snapshot(self, stream: bool = False):
        """
        Toma una foto. Si stream=True devuelve el stream binario (para guardar a archivo).
        """
        return self.send("snapPicture2", stream=stream)

    def get_dev_info(self) -> Optional[str]:
        """Información básica del dispositivo."""
        return self.send("getDevInfo")

    def get_dev_name(self) -> Optional[str]:
        """Nombre del dispositivo."""
        return self.send("getDevName")

    def get_motion_detect_config(self, variant: str = "config") -> Optional[str]:
        """Configuración de detección de movimiento (config / config1 / config2)."""
        cmd_map = {
            "config": "getMotionDetectConfig",
            "config1": "getMotionDetectConfig1",
            "config2": "getMotionDetectConfig2",
        }
        cmd = cmd_map.get(variant, "getMotionDetectConfig")
        return self.send(cmd)

    def get_osd_mask_area(self) -> Optional[str]:
        return self.send("getOsdMaskArea")

    def get_ptz_speed(self) -> Optional[str]:
        return self.send("getPTZSpeed")

    def get_zoom_speed(self) -> Optional[str]:
        return self.send("getZoomSpeed")

    def set_ptz_speed(self, speed: int) -> Optional[str]:
        return self.send("setPTZSpeed", {"speed": int(speed)})

    def zoom_in(self) -> Optional[str]:
        return self.send("zoomIn")

    def zoom_out(self) -> Optional[str]:
        return self.send("zoomOut")

    def zoom_stop(self) -> Optional[str]:
        return self.send("zoomStop")

    def probe_ptz_capabilities(self) -> tuple:
        """Devuelve (has_ptz, has_optical_zoom) según respuestas CGI."""
        has_ptz = False
        has_optical = False
        for cmd, resp in (
            ("getPTZSpeed", self.get_ptz_speed()),
            ("getZoomSpeed", self.get_zoom_speed()),
        ):
            if resp and "error" not in resp.lower():
                if cmd == "getPTZSpeed":
                    has_ptz = True
                else:
                    has_optical = True
        if not has_ptz:
            move = self.send("ptzMoveRight")
            if move and "error" not in move.lower():
                has_ptz = True
            self.ptz_stop()
        return has_ptz, has_optical
