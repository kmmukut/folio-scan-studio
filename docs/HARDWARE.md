# Hardware

Folio works with the printers and scanners that Linux works with: printing
goes through CUPS, scanning through SANE. If a device works with CUPS or
SANE on Linux, it will very likely work with Folio, and Folio offers exactly
the options the device reports.

## Printers

- **Driverless printers** (most network printers from about 2015 onward:
  IPP Everywhere, AirPrint, Mopria) need no driver. In **Add a printer**,
  choose *Driverless*.
- **Older printers** need a driver. Folio's built-in print server includes
  the drivers Debian ships (Gutenprint, HPLIP for HP, foo2zjs, SpliX for
  Samsung and Xerox, the OpenPrinting collections and more), and lists the
  best matches for your model first. With CUPS on another computer, the
  driver must be installed there.
- **Connections:** IPP, JetDirect (port 9100) and LPD over the network;
  USB when the printer is plugged into the host and passed to Folio
  (untested, see below).
- Check a model at the
  [OpenPrinting printer database](https://www.openprinting.org/printers).

## Scanners

- **Network scanners** that speak eSCL (AirScan) or WSD work through
  sane-airscan, which is built in. Add them in **Devices** by IP address.
- **Other scanners** work if a SANE backend supports them; see the
  [SANE supported devices](http://www.sane-project.org/sane-supported-devices.html).
  USB scanners need the device passed into Folio's container (untested,
  see [Deployment](DEPLOYMENT.md#usb-printers-and-scanners)).

## Tested devices

"Tested" means someone ran Folio with the device and reported the result.
Add yours with a
[hardware report](https://github.com/kmmukut/folio-scan-studio/issues/new?template=hardware_report.yml),
including devices that didn't work: knowing what fails is just as useful.

| Device | Type | Connection | Printing | Scanning | Folio | Notes |
|---|---|---|---|---|---|---|
| Samsung M2885FW | Mono laser multifunction | Network | ✓ | ✓ (WSD) | 0.9.x–0.15 | Maintainer's device. Scanner offers flatbed and feeder. Driverless setup reads its capabilities (monochrome, duplex, 17 paper sizes); reports toner, imaging unit, fuser and roller levels. |

### Not yet tested

- **USB printers and scanners.** The maintainers have none, so the USB
  instructions are untested. This is where help is most needed.
- **Raspberry Pi.** The arm64 images are built and tested on 64-bit ARM
  servers, not yet on a Pi.
