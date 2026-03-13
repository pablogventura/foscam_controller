#!/usr/bin/env python3
"""
Consola con autocompletado para probar comandos CGI de cámaras Foscam.
Uso: python cgi_console.py --ip 192.168.1.6 --user admin --password xxx [--port 88]
O:   python cli.py console --ip ... --user ... --password ...
"""

import argparse
import readline
import xml.etree.ElementTree as ET

from foscam.client import FoscamClient

CGI_COMMANDS = [
    "getImageSetting", "setBrightness", "setContrast", "setHue", "setSaturation", "setSharpness",
    "resetImageSetting", "getMirrorAndFlipSetting", "mirrorVideo", "flipVideo", "setPwrFreq",
    "getVideoStreamParam", "setVideoStreamParam",
    "getMainVideoStreamType", "getSubVideoStreamType", "setMainVideoStreamType", "setSubVideoStreamType",
    "getOSDSetting", "setOSDSetting", "getOsdMaskArea", "setOsdMaskArea",
    "getMotionDetectConfig", "setMotionDetectConfig",
    "getSnapConfig", "setSnapConfig", "snapPicture", "snapPicture2",
    "getRecordList", "getAlarmRecordConfig", "setAlarmRecordConfig",
    "setIOAlarmConfig", "getIOAlarmConfig", "clearIOAlarmOutput",
    "getMultiDevList", "getMultiDevDetailInfo", "addMultiDev", "delMultiDev",
    "addAccount", "delAccount", "changePassword", "changeUserName",
    "logIn", "logOut", "getSessionList", "getUserList", "usrBeatHeart",
    "ptzMoveUp", "ptzMoveDown", "ptzMoveLeft", "ptzMoveRight",
    "ptzMoveTopLeft", "ptzMoveTopRight", "ptzMoveBottomLeft", "ptzMoveBottomRight",
    "ptzStopRun", "ptzReset", "getPTZSpeed", "setPTZSpeed",
    "getPTZPresetPointList", "ptzAddPresetPoint", "ptzDeletePresetPoint", "ptzGotoPresetPoint",
    "ptzGetCruiseMapList", "ptzGetCruiseMapInfo", "ptzSetCruiseMap", "ptzDelCruiseMap",
    "ptzStartCruise", "ptzStopCruise",
    "zoomIn", "zoomOut", "zoomStop", "getZoomSpeed", "setZoomSpeed",
    "setPTZSelfTestMode", "getPTZSelfTestMode", "setPTZPrePointForSelfTest", "getPTZPrePointForSelfTest",
    "set485Info", "get485Info",
    "getIPInfo", "setIpInfo", "refreshWifiList", "getWifiList", "setWifiSetting", "getWifiConfig",
    "getPortInfo", "setPortInfo", "getUPnPConfig", "setUPnPConfig",
    "getDDNSConfig", "setDDNSConfig",
    "setFtpConfig", "getFtpConfig", "testFtpServer",
    "getSMTPConfig", "setSMTPConfig", "smtpTest",
    "setSystemTime", "getSystemTime",
    "openInfraLed", "closeInfraLed", "getInfraLedConfig", "setInfraLedConfig",
    "getDevState", "getDevName", "setDevName", "getDevInfo",
    "rebootSystem", "systemReboot",
    "restoreToFactorySetting", "exportConfig", "importConfig", "fwUpgrade",
    "getFirewallConfig", "setFirewallConfig", "getLog",
]

CGI_COMMAND_PARAMS = {
    "setBrightness": "brightness=0~100",
    "setContrast": "contrast=0~100",
    "setHue": "hue=0~100",
    "setSaturation": "saturation=0~100",
    "setSharpness": "sharpness=0~100",
    "mirrorVideo": "isMirror=0|1",
    "flipVideo": "isFlip=0|1",
    "setPTZSpeed": "speed=0~4",
    "ptzAddPresetPoint": "name=nombre",
    "ptzDeletePresetPoint": "name=nombre",
    "ptzGotoPresetPoint": "name=nombre (ej. TopMost, LeftMost)",
    "setDevName": "devName=...",
    "setIpInfo": "isDHCP=0|1 ip=... gate=... mask=... dns1=... dns2=...",
    "setFtpConfig": "ftpAddr=... ftpPort=21 mode=0|1 userName=... password=...",
    "setSystemTime": "timeSource=0|1 ntpServer=... timeZone=... year=... mon=... day=... hour=... minute=... sec=...",
}

PROMPT = "foscam> "


def parse_params(args):
    params = {}
    for s in args:
        if "=" in s:
            k, _, v = s.partition("=")
            params[k.strip()] = v.strip()
    return params


def pretty_xml(text):
    text = (text or "").strip()
    if not text or (not text.lstrip().startswith("<?") and not text.lstrip().startswith("<")):
        return text
    try:
        root = ET.fromstring(text)
        return _xml_to_str(root, 0)
    except ET.ParseError:
        return text


def _xml_to_str(el, indent):
    tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
    text = (el.text or "").strip()
    children = list(el)
    pre = "  " * indent
    if not children and not text:
        return pre + f"<{tag}/>"
    if not children:
        return pre + f"<{tag}>{text}</{tag}>"
    lines = [pre + f"<{tag}>"]
    if text:
        lines.append("  " * (indent + 1) + text)
    for c in children:
        lines.append(_xml_to_str(c, indent + 1))
    lines.append(pre + f"</{tag}>")
    return "\n".join(lines)


def completer(text, state):
    line = readline.get_line_buffer()
    parts = line.split()
    if not parts:
        options = [c for c in CGI_COMMANDS if c.startswith(text)]
    else:
        first = parts[0]
        if len(parts) == 1 and text == first:
            options = [c for c in CGI_COMMANDS if c.startswith(first)]
        else:
            options = []
    options.sort()
    if state < len(options):
        return options[state] + (" " if len(options) == 1 else "")
    return None


def get_cmd_doc(cmd):
    if cmd not in CGI_COMMANDS:
        return None
    return CGI_COMMAND_PARAMS.get(cmd, "  (sin parámetros)")


def main():
    parser = argparse.ArgumentParser(description="Consola CGI Foscam con autocompletado (Tab)")
    parser.add_argument("--ip", required=True, help="IP de la cámara")
    parser.add_argument("--user", required=True, help="Usuario")
    parser.add_argument("--password", required=True, help="Contraseña")
    parser.add_argument("--port", type=int, default=88, help="Puerto (default 88)")
    args = parser.parse_args()

    client = FoscamClient(host=args.ip, user=args.user, password=args.password, port=args.port)

    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(" =")

    print(f"Conectado a {args.ip}:{args.port} (usuario: {args.user})")
    print("Formato: comando [clave=valor ...]  |  help  |  list  |  doc <comando>  |  quit")
    print()

    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0]
        if cmd in ("quit", "exit", "q"):
            break
        if cmd == "help":
            print("  help, list, doc <comando>, quit")
            print("  Parámetros: clave=valor separados por espacios. Ej: setBrightness brightness=70")
            continue
        if cmd == "doc":
            if len(parts) < 2:
                print("Uso: doc <comando>")
                continue
            doc = get_cmd_doc(parts[1])
            print(f"  {parts[1]}: {doc}" if doc else f"  {parts[1]}: (sin parámetros)")
            continue
        if cmd == "list":
            for c in sorted(CGI_COMMANDS):
                hint = CGI_COMMAND_PARAMS.get(c, "")
                print(f"  {c}" + (f"  → {hint}" if hint else ""))
            continue
        if cmd not in CGI_COMMANDS:
            print(f"Comando desconocido: {cmd} (Tab para completar)")
            continue
        cmd_send = "rebootSystem" if cmd == "systemReboot" else cmd
        params = parse_params(parts[1:])
        result = client.send(cmd_send, params)
        if result is None:
            print("(sin respuesta)")
        elif hasattr(result, "read"):
            print("(stream binario; usar snapPicture2 desde script para guardar)")
        else:
            print(pretty_xml(result))
    print("Hasta luego.")


if __name__ == "__main__":
    main()
