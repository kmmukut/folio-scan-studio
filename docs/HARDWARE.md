# Hardware

Folio works with the printers and scanners that Linux works with: printing
goes through CUPS, scanning through SANE. If a device works with CUPS or
SANE on Linux, it will very likely work with Folio, and Folio offers exactly
the options the device reports.

## Printers

- **Driverless printers** (most network printers from about 2015 onward:
  IPP Everywhere, AirPrint, Mopria) need no driver. In **Add a printer**,
  choose *Driverless*. Folio first asks the printer what it accepts:
  driverless printing needs PDF or PWG raster, and some printers that offer
  only AirPrint's own raster print garbled pages. Folio then suggests the
  best-matching driver instead.
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
- **HP multifunction printers** scan through eSCL like the others. HP's own
  SANE plug-in (hpaio) is switched off in the image, because without D-Bus
  it crashes every scanner listing; HP's printer drivers are all included.
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
| Samsung M2885FW | Mono laser multifunction | Network | ✓ | ✓ (WSD) | 0.9.x–0.20 | Maintainer's device. Print with the *Samsung M288x Series PXL* driver (verified over JetDirect): driverless printing garbles pages, because the printer offers only AirPrint's raster (no PDF or PWG raster), and Folio says so. Monochrome, duplex, 17 paper sizes; reports toner, imaging unit, fuser and roller levels. Scanner offers flatbed and feeder, at 75–300 dpi: that is the printer's own limit over WSD (it reports 300 dpi optical resolution there); the 600 dpi on spec sheets needs Samsung's own driver (ULD), which exists only for x86 and finds printers only on its own network. |

## Tested computers

| Computer | System | Folio | Notes |
|---|---|---|---|
| x86-64 server (Proxmox container, Docker) | Debian | 0.9.x–0.20, Full | Maintainer's daily installation. About 180 MB of memory at rest, including the built-in print server. |
| Raspberry Pi 4 Model B (2 GB) | Raspberry Pi OS 64-bit (Debian 13), Docker 29 | 0.20.1, Lite | Installed with Docker's install script and the Quick start; scanning (WSD) and printing to the Samsung M2885FW work. With Folio running, the whole Pi uses about 380 MB of memory. Lite image: 912 MB on disk. The scanner was added by its address (it didn't announce itself to the Pi), as Folio does by default. |

### Not yet tested

- **USB printers and scanners.** The maintainers have none, so the USB
  instructions are untested. This is where help is most needed.
- **Other Raspberry Pi models** (3, 5, 1 GB boards) and other ARM boards.
  The arm64 images are built and tested on 64-bit ARM servers for every
  release; a Pi 4 has been tried (above).
