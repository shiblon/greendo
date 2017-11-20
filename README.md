# greendo - a client library for the RYOBI GDO (Garage Door Opener)

Why GreenDO? Because it's a green door opener.

A Python client for getting status from and manipulating a RYOBI GDO (after setting it up with the official app, first).

The RYOBI GDO is a pretty cool garage door opener with a reasonably nice phone app. But, that's the only way to manipulate it.
There is obviously an API for it, since the phone app has to be using one, but it isn't really obvious what it is. After a large
amount of fiddling, I can now get status and manipulate my unit from the command line, which opens up all kinds of lovely
interoperability possibilities, like cron jobs to change *any* aspect of the door, etc.

## Limitations

The client is not by any means complete. It consists of tools for changing things that I actually have. I only have a fan attached
to mine, so that's what I was able to mess around with. It also doesn't do any notification, though the web socket appears to allow
that (the phone app clearly gets push notifications). I haven't tried messing around with callbacks there, just yet.

I also haven't really done anything with credential storage. The API provides a session cookie, so it's possible to make multiple
requests without logging in and out every time. The client will allow you to do that, certainly, but only within a single command
session. It would be nice to store the cookie in an on-disk cookie jar, along with the API key. That is currently not done, but with
those two bits of information, the client could stay viable for the lifetime of the cookie (which is several days, I believe) without
having to worry abou encrypting a password for storage. None of this has been done yet, but shouldn't be too hard to add.

## Protocol

I don't know the whole protocol, but what is here is likely enough to get any tinkerer going with the missing bits, and hopefully is
clear enough to allow you to get started quickly. The API uses two different connections:

- a regular HTTPS connection for logging in, getting cookies, and retrieiving basic information about the unit and the things connected to it, and
- a web socket (WSS) connection for sending commands to the unit and getting push notifications from it.

The client starts out by logging in via HTTPS, where it gets an API key that can be used to authenticate the web socket.
After that, any command can be issued.
