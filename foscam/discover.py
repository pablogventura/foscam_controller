"""
Descubrimiento de cámaras Foscam en la red local.
Escanea la red buscando dispositivos que respondan al CGI de Foscam.
"""

import socket
import ipaddress
import concurrent.futures
from typing import List, Dict, Optional
from urllib.parse import urlencode
import requests


class FoscamDiscoverer:
    """Descubre cámaras Foscam en la red local."""

    FOSCAM_PORTS = [80, 88, 8080]
    HTTP_TIMEOUT = 2

    def __init__(self, network_range: Optional[str] = None):
        self.network_range = network_range or self._detect_network()
        self.found_cameras: List[Dict] = []

    def _detect_network(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
            return str(network)
        except Exception:
            return "192.168.1.0/24"

    def _is_foscam_camera(self, ip: str, port: int) -> Optional[Dict]:
        url = f"http://{ip}:{port}/cgi-bin/CGIProxy.fcgi"
        test_params = {"cmd": "getDevInfo"}
        test_url = f"{url}?{urlencode(test_params)}"
        try:
            response = requests.get(test_url, timeout=self.HTTP_TIMEOUT)
            if response.status_code == 200:
                content = response.text.lower()
                if "foscam" in content or "devname" in content or "devicename" in content:
                    return {
                        "ip": ip,
                        "port": port,
                        "url": url,
                        "status": "discovered",
                    }
            if response.status_code in (200, 401, 403):
                return {
                    "ip": ip,
                    "port": port,
                    "url": url,
                    "status": "requires_auth",
                }
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, Exception):
            pass
        return None

    def _scan_ip(self, ip: str) -> List[Dict]:
        cameras = []
        for port in self.FOSCAM_PORTS:
            camera = self._is_foscam_camera(ip, port)
            if camera:
                cameras.append(camera)
        return cameras

    def discover(
        self,
        max_workers: int = 50,
        show_progress: bool = True,
    ) -> List[Dict]:
        """Descubre todas las cámaras Foscam en el rango de red."""
        if show_progress:
            print(f"🔍 Escaneando red: {self.network_range}")
            print(f"📡 Puertos: {', '.join(map(str, self.FOSCAM_PORTS))}\n")

        network = ipaddress.IPv4Network(self.network_range, strict=False)
        total_ips = network.num_addresses - 2
        scanned = 0
        self.found_cameras = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_ip = {
                executor.submit(self._scan_ip, str(ip)): ip
                for ip in network.hosts()
            }
            for future in concurrent.futures.as_completed(future_to_ip):
                scanned += 1
                ip = future_to_ip[future]
                if show_progress and scanned % 10 == 0:
                    print(f"⏳ {scanned}/{total_ips} IPs...", end="\r")
                try:
                    cameras = future.result()
                    if cameras:
                        self.found_cameras.extend(cameras)
                        for cam in cameras:
                            icon = "🔓" if cam["status"] == "discovered" else "🔒"
                            print(f"\n{icon} {cam['ip']}:{cam['port']} ({cam['status']})")
                except Exception:
                    pass

        if show_progress:
            print(f"\n✅ Escaneo completado: {len(self.found_cameras)} cámara(s) encontrada(s)\n")
        return self.found_cameras

    def print_results(self) -> None:
        """Imprime los resultados del descubrimiento."""
        if not self.found_cameras:
            print("❌ No se encontraron cámaras Foscam en la red.")
            return
        print(f"\n{'='*60}")
        print(f"📹 CÁMARAS FOSCAM: {len(self.found_cameras)}")
        print(f"{'='*60}\n")
        for i, cam in enumerate(self.found_cameras, 1):
            print(f"  #{i} {cam['ip']}:{cam['port']} — {cam['status']}")
        print()
