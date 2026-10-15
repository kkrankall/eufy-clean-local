"""eufy account login and Tuya local-key lookup.

The eufy -> Tuya login flow is adapted from damacus/robovac
(custom_components/robovac/eufywebapi.py and tuyawebapi.py, Apache License 2.0),
which credits Andre Borie's eufy-device-id-and-local-key-grabber. See NOTICE.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import secrets
import time
import uuid
from hashlib import md5, sha256

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("eufy_cloud")

EUFY_LOGIN_URL = "https://home-api.eufylife.com/v1/user/email/login"
EUFY_CLIENT_ID = "eufyhome-app"
EUFY_CLIENT_SECRET = "GQCpr9dSp3uQpsOMgJ4xQ"
EUFY_HEADERS = {
    "User-Agent": "EufyHome-Android-2.4.0",
    "timezone": "Europe/London",
    "category": "Home",
    "token": "",
    "uid": "",
    "openudid": "sdk_gphone64_arm64",
    "clientType": "2",
    "language": "en",
    "country": "US",
    "Accept-Encoding": "gzip",
}

TUYA_HOSTS = {
    "AZ": "https://a1.tuyaus.com",
    "AY": "https://a1.tuyacn.com",
    "IN": "https://a1.tuyain.com",
    "EU": "https://a1.tuyaeu.com",
}
TUYA_HMAC_KEY = b"A_cepev5pfnhua4dkqkdpmnrdxx378mpjr_s8x78u7xwymasd9kqa7a73pjhxqsedaj"
TUYA_PASSWORD_KEY = bytes([36, 78, 109, 138, 86, 172, 135, 145, 36, 67, 45, 139, 108, 188, 162, 196])
TUYA_PASSWORD_IV = bytes([119, 36, 86, 242, 167, 102, 76, 243, 57, 44, 53, 151, 233, 62, 87, 71])
TUYA_SIGNED_PARAMS = {
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid", "isH5", "h5Token",
    "os", "clientId", "postData", "time", "requestId", "et", "n4h5", "sid", "sp",
}

# Phone codes and countries that live in Tuya's Americas region; everything else
# not listed falls back to Europe, like the eufy app does for most accounts.
AMERICAS_COUNTRIES = {
    "US", "CA", "MX", "BR", "AR", "CL", "CO", "PE", "VE", "EC", "BO", "PY", "UY",
    "AU", "NZ", "JP", "KR", "TW", "HK", "SG", "MY", "TH", "PH", "ID", "VN",
}
REGION_PHONE_CODE = {"AZ": "1", "EU": "44", "AY": "86", "IN": "91"}

CLEANING_APPLIANCE = "Cleaning"


class EufyAuthError(RuntimeError):
    """The eufy account rejected the email or password."""


def _region_for(user_info: dict, settings: dict) -> tuple[str, str]:
    """Pick the Tuya region and phone code for this eufy account."""
    tuya_home = settings.get("setting", {}).get("home_setting", {}).get("tuya_home", {})
    phone_code = str(user_info.get("phone_code") or "")
    region = tuya_home.get("tuya_region_code")
    if not region:
        country = (user_info.get("country") or "").upper()
        if phone_code == "86" or country == "CN":
            region = "AY"
        elif phone_code == "91" or country == "IN":
            region = "IN"
        elif phone_code == "1" or country in AMERICAS_COUNTRIES:
            region = "AZ"
        else:
            region = "EU"
    return region, phone_code or REGION_PHONE_CODE.get(region, "44")


class TuyaSession:
    """Minimal client for the Tuya mobile API that the eufy app logs in to."""

    def __init__(self, uid: str, region: str, phone_code: str, timezone: str) -> None:
        self.uid = uid
        self.phone_code = phone_code
        self.base_url = TUYA_HOSTS.get(region, TUYA_HOSTS["EU"])
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "TY-UA=APP/Android/2.4.0/SDK/null"
        self.params = {
            "appVersion": "2.4.0",
            "deviceId": "8534c8ec0ed0" + secrets.token_hex(16),
            "platform": "sdk_gphone64_arm64",
            "clientId": "yx5v9uc3ef9wg3v9atje",
            "lang": "en",
            "osSystem": "12",
            "os": "Android",
            "timeZoneId": timezone or "Europe/London",
            "ttid": "android",
            "et": "0.0.1",
            "sdkVersion": "3.0.8cAnker",
        }
        self.sid: str | None = None

    @staticmethod
    def _shuffled_md5(value: str) -> str:
        h = md5(value.encode()).hexdigest()
        return h[8:16] + h[0:8] + h[24:32] + h[16:24]

    def _sign(self, params: dict, post_data: str) -> str:
        items = dict(params)
        if post_data:
            items["postData"] = post_data
        parts = []
        for key, value in sorted(items.items()):
            if key in TUYA_SIGNED_PARAMS:
                parts.append(f"{key}={self._shuffled_md5(value) if key == 'postData' else value}")
        return hmac.new(TUYA_HMAC_KEY, "||".join(parts).encode(), sha256).hexdigest()

    def request(self, action: str, data: dict | None = None, version: str = "1.0",
                needs_session: bool = True, _retry: bool = False) -> dict:
        if needs_session and not self.sid:
            self.login()
        params = {
            **self.params,
            "time": str(int(time.time())),
            "requestId": str(uuid.uuid4()),
            "a": action,
            "v": version,
        }
        if self.sid:
            params["sid"] = self.sid
        post_data = json.dumps(data, separators=(",", ":")) if data else ""
        resp = self.http.post(
            self.base_url + "/api.json",
            params={**params, "sign": self._sign(params, post_data)},
            data={"postData": post_data} if post_data else None,
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        if "result" not in body:
            code = str(body.get("errorCode") or "")
            if needs_session and "SESSION" in code.upper() and not _retry:
                self.sid = None  # expired: log in again once
                return self.request(action, data, version, needs_session, _retry=True)
            raise RuntimeError(f"Tuya {action} failed: {code} {body.get('errorMsg')}")
        return body["result"]

    def _derived_password(self) -> str:
        padded = self.uid.zfill(16 * math.ceil(len(self.uid) / 16))
        enc = Cipher(algorithms.AES(TUYA_PASSWORD_KEY), modes.CBC(TUYA_PASSWORD_IV)).encryptor()
        blob = enc.update(padded.encode()) + enc.finalize()
        return md5(blob.hex().upper().encode()).hexdigest()

    def login(self) -> None:
        password = self._derived_password()
        for attempt_password in (password, md5(b"12345678").hexdigest()):
            token = self.request(
                "tuya.m.user.uid.token.create",
                {"uid": self.uid, "countryCode": self.phone_code},
                needs_session=False,
            )
            n = int(token["publicKey"])
            e = int(token["exponent"])
            crypted = pow(int.from_bytes(attempt_password.encode(), "big"), e, n)
            crypted_hex = crypted.to_bytes(math.ceil(n.bit_length() / 8), "big").hex()
            try:
                result = self.request(
                    "tuya.m.user.uid.password.login.reg",
                    {
                        "uid": self.uid,
                        "createGroup": True,
                        "ifencrypt": 1,
                        "passwd": crypted_hex,
                        "countryCode": self.phone_code,
                        "options": '{"group": 1}',
                        "token": token["token"],
                    },
                    needs_session=False,
                )
            except RuntimeError:
                continue
            self.sid = result["sid"]
            self.base_url = result["domain"]["mobileApiUrl"]
            self.params.pop("sid", None)
            return
        raise RuntimeError("Tuya login with the eufy account failed")

    def get_device(self, dev_id: str) -> dict:
        return self.request("tuya.m.device.get", {"devId": dev_id})


class EufyCloud:
    """A logged-in eufy account plus the Tuya session behind it."""

    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.tuya: TuyaSession | None = None
        self._devices: list[dict] = []

    def login(self) -> None:
        resp = requests.post(
            EUFY_LOGIN_URL,
            json={"client_Secret": EUFY_CLIENT_SECRET, "client_id": EUFY_CLIENT_ID,
                  "email": self.email, "password": self.password},
            headers=EUFY_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        login = resp.json()
        if login.get("res_code") != 1:
            raise EufyAuthError(login.get("message") or "eufy login failed")
        user = login["user_info"]
        headers = {**EUFY_HEADERS, "token": login["access_token"], "id": user["id"]}
        host = user["request_host"]
        self._devices = requests.get(host + "/v1/device/v2", headers=headers, timeout=15).json().get("devices", [])
        settings = requests.get(host + "/v1/user/setting", headers=headers, timeout=15).json()
        region, phone_code = _region_for(user, settings)
        self.tuya = TuyaSession("eh-" + user["id"], region, phone_code, user.get("timezone", ""))
        self.tuya.login()

    def devices(self) -> list[dict]:
        """Every robot vacuum on the account, with its Tuya local key."""
        if self.tuya is None:
            self.login()
        found = []
        for item in self._devices:
            if item.get("product", {}).get("appliance") != CLEANING_APPLIANCE:
                continue
            entry = {
                "id": item["id"],
                "model": item["product"]["product_code"],
                "name": item.get("alias_name") or item.get("name"),
                "product_name": item.get("name"),
                "mac": item.get("wifi", {}).get("mac"),
                "local_key": "",
            }
            try:
                # Tuya's "ip" field is the home's public address, so the LAN address
                # comes from UDP discovery or the options instead.
                entry["local_key"] = self.tuya.get_device(item["id"]).get("localKey", "")
            except Exception as err:  # keep the device so the user sees it
                log.warning("Tuya lookup failed for %s (%s): %s", entry["name"], entry["model"], err)
            found.append(entry)
        return found

    def device_dps(self, dev_id: str) -> dict:
        """The cloud's copy of every DP, including the protobuf ones the robot
        leaves out of a local status query."""
        if self.tuya is None:
            self.login()
        return self.tuya.get_device(dev_id).get("dps") or {}

    def media_latest(self, dev_id: str, start: int | str = 0, size: int = 1000) -> dict:
        """The robot's most recent media upload; its "mapId" is the channel it carries."""
        if self.tuya is None:
            self.login()
        return self.tuya.request("tuya.m.device.media.latest",
                                 {"devId": dev_id, "start": start, "size": size}, version="3.0")

    def media_detail(self, dev_id: str, sub_record_id, channel: int,
                     start: int | str = "", size: int = 1000) -> dict:
        """One map channel's uploads, asked for the way the app does: mapId is the channel."""
        if self.tuya is None:
            self.login()
        return self.tuya.request(
            "tuya.m.device.media.detail",
            {"devId": dev_id, "subRecordId": str(sub_record_id), "mapId": channel,
             "start": start, "size": size, "datatype": 0},
            version="3.0",
        )


# Logged-in sessions by email, so the integration can reuse the one the config
# flow just opened: eufy rate-limits logins (HTTP 429) made close together.
SESSIONS: dict[str, EufyCloud] = {}


def fetch_devices(email: str, password: str) -> list[dict]:
    """Log in to eufy and return every robot vacuum with its Tuya local key."""
    return EufyCloud(email, password).devices()
