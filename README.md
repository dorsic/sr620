## SR620
Script for taking measurements with Stanford Research Systems SR620 Universal Time Interval Counter.

The script allows for configuring the counter before starting the measurement or only reading and logging the indicated values.

Indicated values are stored in data files at the primary and secondary locations, which are kept synchronized. The secondary location is meant for detachable devices such as USB flash drives.

### Comminucation With The Instrument

The serial RS-232 interface of the counter is used for communication with the instrument. Only Rx and Tx are used for data transfer. The instrument's default setup, which is recommended, is as follows:
- Baudrate: 9600
- Data bits: 8
- Handshake: None
- Stop bits: 1

This setup is also written as *9600 8N1*.

The default baudrate of 9600 can be changed in the [sr620-default.yml](sr620-default.yml) configuration file, where the default serial device (also known as COM port) is specified as well. The serial device can also be changed using the command line argument `-s` or `--serial-device`.

### Data Logging
Measured values are stored within data files. Data file contains 2 numbers in each row - UTC timestamp of local clock and indicated value
No header line is written to the data file.

```
1688508590.233467	1.00000003622E7
1688508591.279476	1.00000003613E7
1688508592.323591	1.00000003622E7
1688508593.369337	1.00000003613E7
```

#### Data files splitting
The data files are divided into days, so a new file is created for each day.

Data files are created in the form
```
<configurable prefix><4 digit year><3 digit day of year><hour><minute>.txt
```

where the hour and minute being in UTC.

A 3 day long measurement will create 3 files:
- sr620-20231501456.txt
- sr620-20231510000.txt
- sr620-20231520000.txt

#### Data files synchronization
Data files are logged to both a primary location and secondary locations. The primary location folder, as stated in the configuration, must exist before starting the script. The secondary location is used only when the specified folder exists.

When the secondary location becomes available, data files from the primary location with the same prefix, which are not older than max_sync days, are copied to this location.

The secondary location is meant for USB flash drives and may become unavailable during the script execution. This allows for continuous measurements and data file transfer with the help of the USB flash drive.

### Log file
`sr620.log` log file is created on start of the script. Currenct setting the log level is not supported.

### Script Configuration
The script has its own default configuration, and the parameters can be overridden via the [sr620-default.yml](sr620-default.yml) file or command-line arguments. If a setting is specified in both the `yml` file and the command line, the command line takes precedence.

Customized `yml` file can be used with help of the `-c` or `--config` command-line argument, e.g.

```
python3 sr620.py -c sr620-CsI_CsIII.yml
```

The `yml` file allows for specifying command that will be executed uppon opening the serial port and thus configuring the instrument for measurement (see the *instrument-configuration* section) and also commands for reading the measured value (see *read_value_commands* setting)

#### Command-line arguments
```
usage: pps_compare.py [-h] [-c CONFIG] [-s SERIAL_DEVICE] [-t TIMEOUT] [-i] [-l TRIGGER_LEVEL]
                      [-d DATA_PATH] [-p PREFIX] [-u USB_PATH] [-m MAX_HISTORY] [-y MAX_SYNC]

Script logs PPS delay measurement between A-B channels with SR620.

optional arguments:
  -h, --help            show this help message and exit
  -c CONFIG, --config CONFIG
                        Configuration file (default: ./sr620-default.yml)

serial device:
  Serial port device configuration. Other settings are as SR620 defaults - 9600 8N1

  -s SERIAL_DEVICE, --serial_device SERIAL_DEVICE
                        Serial device file pointer. (default: /dev/ttyAMA0)
  -t TIMEOUT, --timeout TIMEOUT
                        Serial device timeout (seconds). (default: 3)

measurement:
  Configuration of the SR620 counter. Defaults are A-B time interval measurement, 1.5V trigger level,
  DC, 50 Ohms for both channels.

  -i, --config_instrument
                        If set configures the measurement instrument upon opening the serial device.
                        (default: False)
  -l TRIGGER_LEVEL, --trigger_level TRIGGER_LEVEL
                        Trigger level for both A and B channel. (default: 1.5V)

data files:
  Data file settings.

  -d DATA_PATH, --data_path DATA_PATH
                        Folder for primary data file location. Must exists. (default: ./data/)
  -p PREFIX, --prefix PREFIX
                        Data file prefix. (default: sr620-)
  -u USB_PATH, --usb_path USB_PATH
                        Folder for second data file location. (default: /media/usb/sr620)
  -m MAX_HISTORY, --max_history MAX_HISTORY
                        Number of days after which data files will be deleted. (default: 999)
  -y MAX_SYNC, --max_sync MAX_SYNC
                        Number of days that will be sync to second data file location (e.g. usb drive)
                        upon connection. (default: 32)

You may disconnect the usb flash drive anytime. Just do not forget to plug it in again.
```
