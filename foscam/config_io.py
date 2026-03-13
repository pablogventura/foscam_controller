"""
Lectura y aplicación de configuración en cámaras Foscam.
Exportar e importar configuración desde/hacia JSON.
"""

import json
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from foscam.client import FoscamClient


# Comandos por categoría para obtener configuración
CONFIG_GET_COMMANDS = {
    "info": [
        "getDevInfo", "getDevName", "getDevState", "getProductName",
        "getSerialNo", "getFirmwareVersion", "getHardwareVersion", "getSoftwareVersion",
    ],
    "network": ["getIPInfo", "getWifiConfig", "getPortInfo", "getDDNSConfig", "getUPnPConfig"],
    "image": [
        "getImageSetting", "getBrightness", "getContrast", "getSaturation",
        "getHue", "getSharpness", "getWhiteBalance", "getExposureMode", "getNightMode", "getFlipMirror",
    ],
    "video": [
        "getVideoStreamParam", "getVideoStreamType", "getVideoEncoderConfig",
        "getFrameRate", "getBitRate", "getResolution",
    ],
    "audio": ["getAudioConfig", "getAudioInputVolume", "getAudioOutputVolume"],
    "motion": ["getMotionDetectConfig", "getMotionDetectConfig1", "getMotionDetectConfig2"],
    "alarm": ["getIOAlarmConfig", "getAlarmRecordConfig", "getAlarmRecordPlan", "getAlarmSchedule"],
    "ftp": ["getFtpConfig", "testFtpServer"],
    "email": ["getEmailConfig", "testEmail"],
    "ptz": ["getPTZSpeed", "getPTZPresetPointList", "getPTZCruiseList", "getPTZSelfTestMode"],
    "system": ["getSystemTime", "getTimeZone", "getNTPConfig", "getSystemLog"],
    "users": ["getUserList"],
    "other": [],
}

GET_TO_SET_MAP = {
    "getDevName": "setDevName",
    "getIPInfo": "setIPInfo",
    "getWifiConfig": "setWifiConfig",
    "getPortInfo": "setPortInfo",
    "getDDNSConfig": "setDDNSConfig",
    "getUPnPConfig": "setUPnPConfig",
    "getImageSetting": "setImageSetting",
    "getBrightness": "setBrightness",
    "getContrast": "setContrast",
    "getSaturation": "setSaturation",
    "getHue": "setHue",
    "getSharpness": "setSharpness",
    "getWhiteBalance": "setWhiteBalance",
    "getExposureMode": "setExposureMode",
    "getNightMode": "setNightMode",
    "getFlipMirror": "setFlipMirror",
    "getVideoStreamParam": "setVideoStreamParam",
    "getVideoStreamType": "setVideoStreamType",
    "getVideoEncoderConfig": "setVideoEncoderConfig",
    "getFrameRate": "setFrameRate",
    "getBitRate": "setBitRate",
    "getResolution": "setResolution",
    "getAudioConfig": "setAudioConfig",
    "getAudioInputVolume": "setAudioInputVolume",
    "getAudioOutputVolume": "setAudioOutputVolume",
    "getMotionDetectConfig": "setMotionDetectConfig",
    "getMotionDetectConfig1": "setMotionDetectConfig1",
    "getMotionDetectConfig2": "setMotionDetectConfig2",
    "getIOAlarmConfig": "setIOAlarmConfig",
    "getAlarmRecordConfig": "setAlarmRecordConfig",
    "getAlarmRecordPlan": "setAlarmRecordPlan",
    "getAlarmSchedule": "setAlarmSchedule",
    "getFtpConfig": "setFtpConfig",
    "getEmailConfig": "setEmailConfig",
    "getPTZSpeed": "setPTZSpeed",
    "getPTZSelfTestMode": "setPTZSelfTestMode",
    "getSystemTime": "setSystemTime",
    "getTimeZone": "setTimeZone",
    "getNTPConfig": "setNTPConfig",
}

CATEGORY_NAMES = {
    "info": "📋 Información del dispositivo",
    "network": "🌐 Red",
    "image": "🖼️ Imagen",
    "video": "🎥 Video",
    "audio": "🔊 Audio",
    "motion": "👁️ Detección de movimiento",
    "alarm": "🚨 Alarmas",
    "ftp": "📤 FTP",
    "email": "📧 Email",
    "ptz": "🎮 PTZ",
    "system": "⚙️ Sistema",
    "users": "👤 Usuarios",
    "other": "📦 Otros",
}


def _parse_xml_response(xml_text: str) -> Dict[str, Any]:
    try:
        root = ET.fromstring(xml_text)
        return {child.tag: child.text for child in root}
    except Exception:
        return {"raw": xml_text}


def _parse_xml_result(xml_text: str) -> Optional[int]:
    try:
        root = ET.fromstring(xml_text)
        result_elem = root.find("result")
        if result_elem is not None and result_elem.text:
            return int(result_elem.text)
    except Exception:
        pass
    return None


def _extract_set_params(get_cmd: str, config_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = {
        k: v for k, v in config_data.items()
        if k not in ("result", "raw", "error") and v is not None
    }
    if get_cmd == "getWifiConfig":
        params = {k: v for k, v in params.items() if k not in ("isConnected", "connectedAP")}
    return params if params else None


class FoscamConfigReader:
    """Lee toda la configuración disponible de una cámara Foscam."""

    def __init__(self, client: FoscamClient):
        self.client = client
        self.config: Dict[str, Dict[str, Dict]] = {}

    def get_all_config(self, show_progress: bool = True) -> Dict[str, Dict[str, Dict]]:
        """Obtiene toda la configuración y la devuelve por categorías."""
        self.config = {cat: {} for cat in CONFIG_GET_COMMANDS}
        total = sum(len(cmds) for cmds in CONFIG_GET_COMMANDS.values())
        current = 0

        if show_progress:
            print(f"📡 Conectando a {self.client.host}:{self.client.port}...\n")

        for category, cmds in CONFIG_GET_COMMANDS.items():
            for cmd in cmds:
                current += 1
                if show_progress and current % 5 == 0:
                    print(f"⏳ {cmd}... ({current}/{total})", end="\r")
                response = self.client.send(cmd)
                if response:
                    parsed = _parse_xml_response(response)
                    self.config[category][cmd] = parsed
                else:
                    self.config[category][cmd] = {"error": "No response"}

        if show_progress:
            print(f"\n✅ Configuración obtenida: {total} comandos\n")
        return self.config

    def print_config(self, config: Optional[Dict] = None) -> None:
        config = config or self.config
        if not config:
            print("❌ No hay configuración para mostrar.")
            return
        print(f"\n{'='*80}")
        print(f"📹 CONFIGURACIÓN — {self.client.host}:{self.client.port}")
        print(f"{'='*80}\n")
        for category, settings in config.items():
            if not settings:
                continue
            print(f"\n{CATEGORY_NAMES.get(category, category)}")
            print("-" * 60)
            for cmd, data in settings.items():
                if isinstance(data, dict) and "error" in data:
                    print(f"  {cmd}: ❌ {data['error']}")
                elif isinstance(data, dict):
                    for k, v in data.items():
                        if k != "raw":
                            print(f"  {cmd}.{k}: {v}")
                else:
                    print(f"  {cmd}: {data}")
        print(f"\n{'='*80}\n")

    def save_to_file(self, filename: str, config: Optional[Dict] = None) -> None:
        config = config or self.config
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"💾 Guardado en: {filename}")


class FoscamConfigWriter:
    """Aplica configuración desde un JSON a una cámara Foscam."""

    def __init__(self, client: FoscamClient):
        self.client = client
        self.applied: List[tuple] = []
        self.failed: List[tuple] = []

    def apply_from_file(
        self,
        json_file: str,
        dry_run: bool = False,
        show_progress: bool = True,
    ) -> Dict[str, int]:
        """Aplica la configuración del archivo JSON. dry_run=True solo muestra qué se aplicaría."""
        if show_progress:
            print(f"📡 Conectando a {self.client.host}:{self.client.port}...")
            if dry_run:
                print("🔍 MODO DRY-RUN: no se aplicarán cambios\n")

        try:
            with open(json_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except FileNotFoundError:
            print(f"❌ No se encontró: {json_file}")
            return {"applied": 0, "failed": 0, "skipped": 0}
        except json.JSONDecodeError as e:
            print(f"❌ JSON inválido: {e}")
            return {"applied": 0, "failed": 0, "skipped": 0}

        self.applied = []
        self.failed = []
        skipped: List[str] = []
        total = 0

        for category, commands in config.items():
            if not isinstance(commands, dict):
                continue
            for get_cmd, config_data in commands.items():
                if not isinstance(config_data, dict) or "error" in config_data:
                    skipped.append(get_cmd)
                    continue
                set_cmd = GET_TO_SET_MAP.get(get_cmd)
                if not set_cmd:
                    skipped.append(get_cmd)
                    continue
                params = _extract_set_params(get_cmd, config_data)
                if not params:
                    skipped.append(get_cmd)
                    continue
                total += 1
                if dry_run:
                    if show_progress:
                        print(f"  [DRY] {set_cmd} {params}")
                    self.applied.append((get_cmd, set_cmd, params))
                else:
                    if show_progress:
                        print(f"⏳ {set_cmd}...", end="\r")
                    response = self.client.send(set_cmd, params)
                    if response is not None:
                        result = _parse_xml_result(response)
                        if result == 0:
                            if show_progress:
                                print(f"✅ {set_cmd}")
                            self.applied.append((get_cmd, set_cmd, params))
                        else:
                            if show_progress:
                                print(f"⚠️ {set_cmd} → código {result}")
                            self.failed.append((get_cmd, set_cmd, params, str(result)))
                    else:
                        if show_progress:
                            print(f"❌ {set_cmd}")
                        self.failed.append((get_cmd, set_cmd, params, "sin respuesta"))

        if show_progress:
            print(f"\n📊 Aplicados: {len(self.applied)} | Fallidos: {len(self.failed)} | Omitidos: {len(skipped)}\n")
        return {
            "applied": len(self.applied),
            "failed": len(self.failed),
            "skipped": len(skipped),
            "total": total,
        }
