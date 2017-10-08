#!/usr/bin/env python2

"""Provide a basic client for accessing functionality for the RYOBI GDO.

Can be used on the command line by specifying a command and optional values for it, e.g.,

> python greendo.py door open
> python greendo.py light off
> python greendo.py status door

See help for more details.
"""

from __future__ import print_function, division

import argparse
import cookielib
import json
import sys
import urllib2
import websocket

from getpass import getpass
from pprint import pprint, pformat
from collections import namedtuple
from contextlib import closing

class _Response(namedtuple("_Response", "code error data raw")):
    """Holds useful data from web responses, making it easier to detect errors, etc.

    Attributes:
        code: The HTTP response code.
        error: The error message, if any, sometimes as serialized JSON.
        data: The deserialized response data.
        raw: The raw response text.
    """
    @classmethod
    def from_url_resp(cls, resp):
        raw = resp.read()
        data = {}
        if raw:
            data = json.loads(raw)
        code = resp.getcode()
        if code != 200:
            return cls(code=code, error=code, data=data, raw=raw)
        if data.get("err") is not None:
            return cls(code=code, error=data, data=data, raw=raw)
        result = data.get("result")
        if not result:
            return cls(code=code, error="No result", data=data, raw=raw)
        return cls(code=code, error=None, data=data, raw=raw)

class Session(namedtuple("Session", "api_key data")):
    """Session information, notably the api key needed for web socket work.

    Attributes:
        api_key: The key to be used with the web socket.
        data: The deserialized JSON response.
    """

class _Attr(object):
    """An attribute item, taken from device details.

    An example is "garageDoor_8". That's a key in the device details map
    and it points to a large dictionary of metadata.

    Attributes:
        key: The key in the device attributes map.
        data: The data for this attribute.
    """
    def __init__(self, key, data):
        self.key = key
        self.data = data

    def maybe(self, *path):
        """Try to drill into a nested dictionary using a key name path.

        Args:
            *path: A list of keys, each a layer deeper in a nested dict.

        Returns:
            A value, if found, or None.
        """
        if not self.valid():
            return None
        val = self.data
        for p in path:
            val = val.get(p)
            if val is None:
                return None
        return val

    def valid(self):
        """Indicate whether this object has any data."""
        return self.data is not None

class _Module(_Attr):
    """A GDO module, like the light, fan, etc."""

    def port(self):
        """Return the port ID for use with web socket commands."""
        return self.maybe("portId", "value")

    def module(self):
        """Return the module ID for use with web socket commands."""
        return self.maybe("moduleId", "value")

class _Charger(_Module):
    """A charger module, used for getting battery level."""
    def level(self):
        """Returns the charge level as an integer from 0 to 100."""
        return self.maybe("chargeLevel", "value")

class _Door(_Module):
    """A door module, used to get status about position, state, etc."""

    OPENING = "opening"
    CLOSING = "closing"
    OPEN = "open"
    CLOSED = "closed"
    STATIONARY = "stationary"
    ERROR = "error"
    LOCKED = "locked"

    def max_pos(self):
        """Returns the maximum door position in some units (inches?)."""
        return self.maybe("maxDoorPosition", "value")

    def preset_pos(self):
        """Returns the current position in some units (inches?)."""
        return self.maybe("presetPosition", "value")

    def alarm(self):
        """Returns the alarm state of the door."""
        return self.maybe("alarmState", "value")

    def motor(self):
        """Returns the motor status of the door."""
        return self.maybe("motorStatus", "value")

    def motion(self):
        """Returns the state of the motion sensor (on or off)."""
        return self.maybe("motionSensor", "value")

    def sensor(self):
        """Returns the state of the safety sensors."""
        return self.maybe("sensorFlag", "value")

    def vacation(self):
        """Indicates whether vacation mode is on."""
        return self.maybe("vacationMode", "value")

    def door_status(self):
        """Returns the status of the door (opening, closing, open, closed)."""
        state = self.maybe("doorState", "value")
        if state == 0:
            return self.CLOSED
        elif state == 1:
            return self.OPEN
        elif state == 2:
            return self.CLOSING
        else:
            return self.OPENING

    def door_error(self):
        """Returns the type of error last encountered, if any."""
        mode = self.maybe("opMode", "value")
        if mode == 0:
            return None
        elif mode == 1:
            return self.ERROR
        elif mode == 2:
            return self.LOCKED

    def door_max(self):
        """Returns the maximum position of the door in some units (inches?)."""
        return self.maybe("maxDoorPosition", "value")

    def door_pos(self):
        """Returns the position of the door in some units (inches?)."""
        m = self.door_max()
        return self.maybe("doorPosition", "value")

class _Fan(_Module):
    """A fan module, used for getting the speed."""

    def speed(self):
        """Return the speed in an integer from 0 to 100."""
        return self.maybe("speed", "value")

class _Light(_Module):
    """A light module, used for getting state and timing."""

    def on(self):
        """Indicates whether the light is on."""
        return self.maybe("lightState", "value")

    def timer(self):
        """Returns the auto-off delay in minutes."""
        return self.maybe("lightTimer", "value")

class Device(object):
    """A device, meaning the complete garage door opener unit with all its stuff.

    Attributes:
        meta: Device metadata dictionary taken from the master device list.
        data: Device details dictionary for this particular device.
        charger: The charger module.
        door: The door module (this has most of the information).
        master: The masterUnit module.
        fan: The fan module, if any. Note, it only supports one - that might need to change.
        wifi: The wifi module.
        light: The light module.
    """

    def __init__(self, meta, data):
        self.meta = meta
        self.data = data

        self.charger = None
        self.door = None
        self.master = None
        self.fan = None
        self.wifi = None
        self.light = None

        for k, v in self.data["attributes"].iteritems():
            if k == "masterUnit":
                self.master = _Attr(k, v)
            elif k.startswith("backupCharger_"):
                self.charger = _Charger(k, v)
            elif k.startswith("garageDoor_"):
                self.door = _Door(k, v)
            elif k.startswith("fan_"):
                self.fan = _Fan(k, v)
            elif k.startswith("wifiModule_"):
                self.wifi = _Module(k, v)
            elif k.startswith("garageLight_"):
                self.light = _Light(k, v)
            else:
                # Not fatal, just something we haven't encountered.
                print("Unknown module key {!r}".format(k))

    def _module_cmd_payload(self, module, msg):
        """Generate a command payload for sending mutation commands to the web socket."""
        return {
            "jsonrpc": "2.0",
            "method": "gdoModuleCommand",
            "params": {
                "msgType": 16,
                "moduleType": module.module(),
                "portId": module.port(),
                "topic": self.id,
                "moduleMsg": msg,
            }
        }

    @property
    def id(self):
        return self.meta["varName"]

    @property
    def name(self):
        return self.meta["name"]


    def cmd_open(self):
        return self._module_cmd_payload(self.door, {
            "doorCommand": "1",
        })

    def cmd_close(self):
        return self._module_cmd_payload(self.door, {
            "doorCommand": "0",
        })

    def cmd_preset(self):
        return self._module_cmd_payload(self.door, {
            "doorCommand": "2",
        })

    def cmd_preset_pos(self, pos):
        return self._module_cmd_payload(self.door, {
            "presetPosition": max(0, min(self.door.max_pos(), int(pos)))
        })

    def cmd_light(self, on):
        return self._module_cmd_payload(self.light, {
            "lightState": bool(on),
        })

    def cmd_light_timer(self, time):
        return self._module_cmd_payload(self.light, {
            "lightTimer": int(time),
        })

    def cmd_vacation(self, on):
        return self._module_cmd_payload(self.vacation, {
            "vacationMode": bool(on),
        })

    def cmd_motion(self, on):
        return self._module_cmd_payload(self.door, {
            "motionSensor": bool(on),
        })

    def cmd_fan(self, speed):
        return self._module_cmd_payload(self.fan, {
            "speed": min(100, max(0, int(speed))),
        })

class ResponseError(Exception):
    """Raised when there is a response error."""

    def __init__(self, reason, data):
        self.reason = reason
        self.data = data
        super(ResponseError, self).__init__("{}: {!r}".format(reason, data))

class Client(object):
    """A client for talking to the GDO.

    Attributes:
        username: The email addressed used for authentication.
        session: Session information, including api_key.
        devices: A list of all Device objects known to the server.
    """

    API_URL_PREFIX = "https://tti.tiwiconnect.com/api"
    API_URL_SOCKET = "wss://tti.tiwiconnect.com/api/wsrpc"

    def __init__(self, username, password):
        self._cookie_jar = cookielib.CookieJar()
        self._opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(self._cookie_jar))

        self.username = username
        self.session = self._login(username, password)
        self.devices = self._devices()

        self.master = None
        for d in self.devices:
            if d.master is not None:
                self.master = d.master
                break

        if not self.master:
            raise ValueError("couldn't find master unit")

        self.ws = websocket.create_connection(self.API_URL_SOCKET)
        self.ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "srvWebSocketAuth",
            "params": {
                "varName": username,
                "apiKey": self.session.api_key,
            },
        }))
        try:
            ws_auth = json.loads(self.ws.recv())
            if not ws_auth:
                raise ValueError("no socket auth returned")
            params = ws_auth.get("params")
            if not params:
                raise ValueError("no socket auth params received")
            authorized = params.get("authorized")
            if not authorized:
                raise ValueError("socket not authorized: {}".format(pformat(ws_auth)))
        except:
            self.close()
            raise

    def _send_request(self, path, data=None):
        if not path.startswith("/"):
            path = "/" + path

        data_str = None
        if data:
            data_str = json.dumps(data)

        headers = {
            "x-tc-transform": "tti-app",
        }
        if data_str:
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(data_str))
        else:
            headers["x-tc-transformversion"] = "0.2"

        req = urllib2.Request(self.API_URL_PREFIX + path, data=data_str, headers=headers)
        return _Response.from_url_resp(self._opener.open(req))

    def _login(self, username, password):
        resp = self._send_request("/login", data={
            "username": username,
            "password": password,
        })
        if resp.error:
            raise ResponseError("login failed", resp)
        data = resp.data["result"]
        return Session(api_key=data["auth"]["apiKey"], data=data)

    def close(self):
        """Close both the HTTPS and WSS connections."""
        self.ws.close()
        resp = self._send_request("/logout")
        if resp.error:
            raise ResponseError("logout failed", resp)
        return True

    def _devices(self):
        resp = self._send_request("/devices")
        if resp.error:
            raise ResponseError("devices request failed", resp)
        meta = resp.data["result"]

        devices = []
        for device in meta:
            name = device["varName"]
            dresp = self._send_request("/devices/" + name)
            if dresp.error:
                raise ResponseError("device request failed for {}".format(name), resp)
            # TODO: can there ever be more than one?
            devices.append(Device(meta=device, data=dresp.data["result"][0]))
        return devices

    def send_command(self, cmd):
        """Send a comand to the unit for a particular device.

        Use the Device command functions to generate appropriate commands.
        """
        self.ws.send(json.dumps(cmd))
        return json.loads(self.ws.recv())

    @property
    def api_key(self):
        return self.session.api_key

    @property
    def tz_offset(self):
        return self.master.maybe("timeZoneOffset", "value")

def main():
    ap = argparse.ArgumentParser(prog="greendo")
    ap.add_argument("--email", "-u", type=str, help="Email address registered with the GDO app. Default: request from stdin.")
    ap.add_argument("--pwd", "-p", type=str, help="Password for the registered email. Default: request from stdin.")
    ap.add_argument("--dry", "-n", action="store_true", help="Dry run - don't execute commands, just display them")
    ap.add_argument("--dev", "-d", type=int, default=0, help="Door opener device index, if you have more than one.")
    sub_ap = ap.add_subparsers(dest="target", help="Commands")

    ap_status = sub_ap.add_parser("status", help="Output status for a given subsystem.")
    ap_status.add_argument("thing", choices=("config", "charger", "door", "light", "fan"),
                           help="Get status for the given subsystem.")

    ap_door = sub_ap.add_parser("door", help="Manipulate the door: open, close, preset.")
    ap_door.add_argument("cmd", choices=("open", "close", "preset"))

    ap_motion = sub_ap.add_parser("motion", help="Turn the motion sensor on or off.")
    ap_motion.add_argument("set", choices=("on", "off"))

    ap_light = sub_ap.add_parser("light", help="Turn the light on or off.")
    ap_light.add_argument("set", choices=("on", "off"))

    ap_light_timer = sub_ap.add_parser("lighttimer", help="Set the number of minutes for the light timer.")
    ap_light_timer.add_argument("minutes", type=int)

    ap_fan = sub_ap.add_parser("fan", help="Set fan to integer speed 0-100 (0 is off)")
    ap_fan.add_argument("speed", type=int)

    ap_vacation = sub_ap.add_parser("vacation", help="Turn vacation mode on or off.")
    ap_vacation.add_argument("set", choices=("on", "off"))

    ap_preset_pos = sub_ap.add_parser("preset", help="Set the preset position in integer inches.")
    ap_preset_pos.add_argument("inches", type=int)

    args = ap.parse_args()

    email = args.email
    pwd = args.pwd
    if args.email is None:
        email = raw_input("email: ").strip()
    if args.pwd is None:
        pwd = getpass("password: ").strip()

    with closing(Client(email, pwd)) as client:
        device = client.devices[max(0, min(args.dev, len(client.devices)))]
        cmd = None
        if args.target == "status":
            thing = args.thing
            if thing == "config":
                print("Session:\n", json.dumps(client.session.data, indent=2))
                print("Devices:\n", json.dumps([{"meta": d.meta, "data": d.data} for d in client.devices], indent=2))
            elif thing == "charger":
                print(json.dumps({
                    "level": device.charger.level()
                }, indent=2))
            elif thing == "door":
                door = device.door
                print(json.dumps({
                    "status": door.door_status(),
                    "error": door.door_error(),
                    "pos": door.door_pos(),
                    "max": door.door_max(),
                    "preset": door.preset_pos(),
                    "motion": door.motion(),
                    "alarm": door.alarm(),
                    "motor": door.motor(),
                    "sensor": door.sensor(),
                    "vacation": door.vacation(),
                }, indent=2))
            elif thing == "light":
                light = device.light
                print(json.dumps({
                    "light": light.on(),
                    "timer": light.timer(),
                }, indent=2))
            elif thing == "fan":
                print(json.dumps({
                    "speed": device.fan.speed(),
                }, indent=2))
            return

        if args.target == "door":
            if args.cmd == "open":
                cmd = device.cmd_open()
            elif args.cmd == "close":
                cmd = device.cmd_close()
            else:
                cmd = device.cmd_preset()
        elif args.target == "motion":
            cmd = device.cmd_motion(args.set == "on")
        elif args.target == "light":
            cmd = device.cmd_light(args.set == "on")
        elif args.target == "lighttimer":
            cmd = device.cmd_lighttimer(max(0, args.minutes))
        elif args.target == "fan":
            cmd = device.cmd_fan(max(0, min(100, args.speed)))
        elif args.target == "vacation":
            cmd = device.cmd_vacation(args.set == "on")
        elif args.target == "preset":
            cmd = device.cmd_preset(max(0, args.inches))

        if args.dry:
            print("Dry Run:")
            print(json.dumps(cmd, indent=2))
            return

        print("Request to {}:".format(client.API_URL_SOCKET))
        print(json.dumps(cmd, indent=2))

        result = client.send_command(cmd)
        print("Response:")
        print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
