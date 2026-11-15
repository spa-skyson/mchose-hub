# Changelog

## 0.3.1

- Fixed duplicated HID report IDs in headset command and channel-control frames. The transport now adds the report ID once; battery, audio mode, and headset settings can be read from the dongle.
- Corrected the documented explanation for prior command timeouts: malformed frames from earlier application versions caused the missing responses; the dongle command channel does not park after inactivity.
- Documented channel open/close lifecycle, passive status polling every 15 seconds, battery refresh no more often than every five minutes, and full refresh after connection or recovery.
- Documented automatic dongle rediscovery after `EIO`, `ENODEV`, or EOF, reconnection on a new HID node, and stale battery age labels.
- Removed local machine paths from the protocol source references; retained the public M HUB web-bundle URL and file name.
