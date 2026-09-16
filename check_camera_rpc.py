#!/usr/bin/env python3
import hashlib
import os
import requests
from requests.auth import HTTPDigestAuth

CAMERA_USER = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASS = os.environ.get("CAMERA_PASS", "")
IPS = ["10.30.0.201", "10.30.0.202"]
CONFIG_NAMES = [
    "MotionDetect",
    "VideoAnalyseRule",
    "VideoMotion",
    "VideoMotion[0]",
    "VideoMotion.Channel[0]",
    "MotionDetect[0]",
    "VideoDetect",
    "VideoDetection",
    "SmartMotion",
    "SmartMotionHuman",
    "SmartMotionVehicle",
    "IVS",
    "VideoInputChannel",
    "VideoInputChannel[0]",
    "AlarmHost",
    "Alarm",
    "General",
    "Encode",
    "VideoInMode",
    "VideoInput",
    "Channel",
    "Image",
    "Storage",
]

CONFIG_PARAM_VARIANTS = [
    lambda name: {"name": name},
    lambda name: {"name": name, "channel": 0},
    lambda name: {"name": name, "table": {"Channel": 0}},
    lambda name: {"name": name, "table": [0]},
]


def dahua_login(ip: str, user: str, pw: str):
    base = f"https://{ip}"
    challenge = requests.post(
        f"{base}/RPC2_Login",
        json={
            "method": "global.login",
            "params": {"userName": user, "password": "", "clientType": "Web3.0"},
            "id": 1,
        },
        timeout=10,
        verify=False,
    )
    challenge.raise_for_status()
    challenge_json = challenge.json()
    params = challenge_json.get("params") or {}
    realm = params.get("realm")
    random_ = params.get("random")
    session = challenge_json.get("session")
    print(f"[{ip}] challenge: realm={realm}, random={random_}, session={session}")
    if not realm or not random_ or not session:
        print(f"[{ip}] missing challenge fields: {challenge_json}")
        return None

    h1 = hashlib.md5(f"{user}:{realm}:{pw}".encode()).hexdigest().upper()
    h2 = hashlib.md5(f"{user}:{random_}:{h1}".encode()).hexdigest().upper()

    auth = requests.post(
        f"{base}/RPC2_Login",
        json={
            "method": "global.login",
            "params": {"userName": user, "password": h2, "clientType": "Web3.0"},
            "id": 2,
            "session": session,
        },
        timeout=10,
        verify=False,
    )
    auth.raise_for_status()
    auth_json = auth.json()
    print(f"[{ip}] auth response: {auth_json}")
    result = auth_json.get("result")
    if not result:
        print(f"[{ip}] auth failed: {auth_json}")
        return None

    permanent_session = auth_json.get("session")
    print(f"[{ip}] logged in; session={permanent_session}")
    return permanent_session


def get_config(ip: str, session: str, name: str, params=None):
    payload = {
        "method": "configManager.getConfig",
        "params": params or {"name": name},
        "id": 101,
        "session": session,
    }
    resp = requests.post(f"https://{ip}/RPC2", json=payload, timeout=15, verify=False)
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:2000]
    return resp.status_code, body


def cgi_get_config(ip: str, user: str, pw: str, name: str):
    url = f"https://{ip}/cgi-bin/configManager.cgi?action=getConfig&name={name}"
    resp = requests.get(url, auth=HTTPDigestAuth(user, pw), timeout=15, verify=False)
    return resp.status_code, resp.text[:2000]


if not CAMERA_PASS:
    print("CAMERA_PASS is not set. Source /etc/camera-alert.env or export it before running this script.")
    raise SystemExit(1)

for ip in IPS:
    print(f"\n=== {ip} ===")
    session = dahua_login(ip, CAMERA_USER, CAMERA_PASS)
    if not session:
        continue
    for name in CONFIG_NAMES:
        for variant_idx, make_params in enumerate(CONFIG_PARAM_VARIANTS, start=1):
            params = make_params(name)
            status, body = get_config(ip, session, name, params=params)
            print(f"[{name}] variant={variant_idx} params={params} status={status}")
            print(body)
            print("---")
        cgi_status, cgi_body = cgi_get_config(ip, CAMERA_USER, CAMERA_PASS, name)
        print(f"[{name}] CGI status={cgi_status}")
        print(cgi_body)
        print("===")
