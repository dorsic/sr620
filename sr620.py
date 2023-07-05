import argparse
import logging
import os
import re
import shutil
import yaml
from enum import Flag
from serial import Serial
from datetime import datetime
from time import sleep
from threading import Thread

logfile = 'sr620.log'

default_config = {
    "serial_connection": {
        "device": "/dev/ttyAMA0",
        "timeout": 3,
        "baudrate": 9600
    },
    "primary_data_path": "./data",
    "secondary_data_path": "/media/usb/sr620",
    "prefix": "sr620-",
    "max_history": 999,
    "max_sync": 32,
    "instrument_configuration": [
        {"command": "*RST", "desc": "Counter reset and prepared for configuration."},
        {"command": "MODE 0;SIZE 1;SRCE 0", "desc": "Time mode selected, sample size 1, source A."},
        {"command": "TCPL 1,0;TCPL 2,0", "desc": "Channels A and B set to be DC coupled."},
        {"command": "TERM 1,0;TERM 2,0", "desc": "Channels A and B set to be 50 Ohm terminated."},
        {"command": "TMOD 1,0;TMOD 2,0", "desc": "Channels A and B trigger mode normal."},
        {"command": "TSLP 1,0;TSLP 2,0", "desc": "Channels A and B trigger slope positive."},
        {"command": "LEVL 1,1.5;LEVL 2,1.5", "desc": "Trigger levels of channels A and B set to 1.5V."},
        {"command": "ARMM 0;AUTM 1;DREL 0", "desc": "Arming mode +-time, automode on, A and B trigger slope positive."}
        ],
    "configure_upon_start": False,
    "start_measurement_immediately": True,
    "read_value_commands": "*WAI;XAVG?"
}

class StateFlag(Flag):
    NONE = 0
    SERIAL = 1
    USB = 2
    DUAL_WRITE = 4
    SYNCING_FILES = 8
    DELETING_FILES = 16
    BUFFERING = 32

class DualFileData():

    sep = '\t'

    def __init__(self, primary_data_folder:str, file_prefix:str, max_history:int, max_sync:int, secondary_data_folder:str=None):
        self.primary_data_folder = primary_data_folder
        self.file_prefix = file_prefix
        self.max_history = max_history
        self.max_sync = max_sync
        self.secondary_data_folder = secondary_data_folder
        self.fprimary = None
        self.fsecondary = None
        self.fdoy = 0
        self._state = StateFlag.NONE
        self._newstate_listeners = []
        self.sync_thread = None
        self.delete_thread = None
        self.data_file_pattern = f"^{self.file_prefix}[0-9]{{11}}.txt$"
        self.databuffer = []

    def _clearflag(self, flag):
        must_notify=False
        for f in StateFlag:
            if f == StateFlag.NONE:
                continue
            if f in flag and f in self._state:
                self._state &= ~f
                must_notify = True
        if must_notify:
            self._notify_newstate()

    def _setflag(self, flag):
        if flag in self._state:
            return
        self._state = self._state | flag
        self._notify_newstate()

    def register_newstatelistener(self, callback):
        self._newstate_listeners.append(callback)

    def _notify_newstate(self):
        for foo in self._newstate_listeners:
            foo()

    @property
    def filename(self):
        dt = datetime.utcnow()
        return f'{self.file_prefix}{dt.strftime("%Y%j%H%M")}.txt'

    @property
    def doy(self):
        dt = datetime.utcnow()
        return int(dt.strftime("%j"))

    def open(self, secondary=False):
        # we want the program crash if primary file cound not be opened
        if not secondary:
            self.fprimary = open(os.path.join(self.primary_data_folder, self.filename), 'a', buffering=1)
        try:
            if secondary:
                if self._exists(self.secondary_data_folder):
                    self._setflag(StateFlag.USB)
                    self.fsecondary = open(os.path.join(self.secondary_data_folder, os.path.basename(self.fprimary.name)), 'a', buffering=1)
                else:
                    self._clearflag(StateFlag.USB | StateFlag.DUAL_WRITE | StateFlag.SYNCING_FILES)
        except Exception as e:
            logging.exception(f"Unhandled exception in DualDataFile.open() secondary={secondary}", exc_info=e , stack_info=True)

    def close(self):
        try:
            if self.fprimary:
                logging.debug("Closing primary file...")
                self.fprimary.close()
                logging.debug("Primary file closed.")
        except Exception as e:
            logging.exception("Unhandled exception in DualDataFile.close() primary file", exc_info=e , stack_info=True)
        try:
            if self.fsecondary:
                logging.debug("Closing secondary file...")
                self.fsecondary.close()
                logging.debug("Secondary file closed.")
        except Exception as e:
            logging.exception("Unhandled exception in DualDataFile.close() secondary file", exc_info=e , stack_info=True)
        finally:
            self._clearflag(StateFlag.DUAL_WRITE)

    def _sync(self):
        doy = self.doy
        for f in os.scandir(self.primary_data_folder):
            try:
                if f.is_file() and re.search(pattern=self.data_file_pattern, string=f.name) is not None:
                    fdoy = int(f.name[-11:-8])
                    if (doy-fdoy < self.max_sync) and ( \
                            (not self._exists(os.path.join(self.secondary_data_folder, f.name))) or \
                            (os.path.getsize(os.path.join(self.secondary_data_folder, f.name)) != os.path.getsize(os.path.join(self.primary_data_folder, f.name)))
                    ):
                        # copy file
                        logging.debug(f"Copying file {f.name}")
                        if doy == fdoy:
                            # do not modify the data file while copying
                            self._setflag(StateFlag.BUFFERING)
                        shutil.copyfile(f.path, os.path.join(self.secondary_data_folder, f.name))
                        logging.info(f"Copied file {f.name}")
                        if doy == fdoy:
                            self._clearflag(StateFlag.BUFFERING)
                else:
                    logging.debug(f"Skipping file {f.name}")
            except Exception as e:
                logging.exception(f"Unable to copy file {f}", exc_info=e, stack_info=True)
                self._clearflag(StateFlag.BUFFERING)

        self.sync_thread = None
        self._clearflag(StateFlag.SYNCING_FILES)
        logging.info("Sync files done.")
        self.open(secondary=True)
        
    def _offload_sync(self):
        try:
            if StateFlag.SYNCING_FILES in self._state or self.sync_thread is not None:
                logging.warning("Want to start syncing files, but a proces is already underway.")
                return
            
            self._setflag(StateFlag.SYNCING_FILES)
            self.sync_thread = Thread(target=self._sync)
            logging.info("Starting new thread for syncing files...")
            self.sync_thread.start()
        except Exception as e:
            logging.exception("Unable to start new thread for syncing files.", exc_info=e, stack_info=True)

    def _delete(self):
        doy = self.doy
        for f in os.scandir(self.primary_data_folder):
            try:
                if f.is_file() and re.search(pattern=self.data_file_pattern, string=f.name) is not None:
                    fdoy = int(f[-11:-8])
                    if fdoy - doy > self.max_history:
                        os.remove(f.path)
                        logging.debug(f"Deleted file {f.name}")
            except Exception as e:
                logging.exception(f"Unable to delete file {f}", exc_info=e, stack_info=True)

        self.delete_thread = None
        self._clearflag(StateFlag.DELETING_FILES)
        logging.info("Deletion of files done.")
        

    def _offload_delete(self):
        try:
            if StateFlag.SYNCING_FILES in self._state or self.sync_thread is not None:
                logging.warning("Want to start file deletion, but a proces is already underway.")
                return
            self._setflag(StateFlag.DELETING_FILES)
            self.delete_thread = Thread(target=self._delete)
            logging.info("Starting new thread for deleting files...")
            self.delete_thread.start()
        except Exception as e:
            logging.exception("Unable to start new thread for file deletion.", exc_info=e, stack_info=True)

    @staticmethod
    def _exists(path):
        if os.path.isdir(path):
            #logging.debug(f"Path {path} of base {os.path.basename(path)} of dir {os.path.dirname(path)} is in {list(os.scandir(os.path.dirname(path)))}")
            return os.path.basename(path) in [f.name for f in os.scandir(os.path.dirname(path))]
        if os.path.isfile(path):
            return True
        return False

    def write(self, data):
        if self.fdoy != self.doy:
            # new day, we need to open a new file
            fsec = self.fsecondary is not None
            self.close()
            self.open(secondary=False)
            self.fdoy = self.doy
            if fsec:
                self.open(secondary=True)
            self._offload_delete()

        ts = datetime.utcnow().timestamp()
        sbuffer = []
        try:
            if self.fprimary:
                if StateFlag.BUFFERING in self._state:
                    self.databuffer.append(f"{ts}{self.sep}{data}")
                    return
                else:
                    for bdata in self.databuffer:
                        self.fprimary.write(f"{bdata}\n")
                    sbuffer = self.databuffer
                    self.databuffer = []
                    self.fprimary.write(f"{ts}{self.sep}{data}\n")
                    #self.fprimary.flush()
        except Exception as e:
            logging.exception("Unhandled exception in DualDataFile.write() primary file", exc_info=e , stack_info=True)

        try:
            if self._exists(self.secondary_data_folder):
                self._setflag(StateFlag.USB)
                if StateFlag.SYNCING_FILES in self._state:
                    return
                else:
                    if not self.fsecondary:
                        self._offload_sync()
                        return
                    else:
                        try:
                            for bdata in sbuffer:
                                self.fsecondary.write(f"{bdata}\n")
                            self.fsecondary.write(f"{ts}{self.sep}{data}\n")
                            #self.fsecondary.flush()
                            self._setflag(StateFlag.DUAL_WRITE)
                        except:
                            # flash drive removed
                            logging.WARNING("Secondary file unaccesible.")
                            self._clearflag(StateFlag.DUAL_WRITE)
                            self.fsecondary = None

            else:
                self._clearflag(StateFlag.USB | StateFlag.DUAL_WRITE | StateFlag.SYNCING_FILES)
                if self.fsecondary:
                    self.fsecondary = None
                    logging.debug("Lost handle to secondary file.")
                self.sync_thread = None
        except Exception as e:
            logging.exception("Unhandled exception in DualDataFile.write() secondary file", exc_info=e , stack_info=True)

class SR620():

    def __init__(self, dualdatafile, config):
        self.config = config
        self._state = StateFlag.NONE
        self._read_error_count = 0
        self.datafile = ddf
        self.datafile.register_newstatelistener(self._fnewstate)

    def _fnewstate(self):
        logging.debug(f"New state {self.state}")

    @property
    def state(self):
        ret = []
        for e in StateFlag:
            if e == StateFlag.NONE:
                continue
            #ret.append(e.name if e in (self._state | self.datafile._state) else 'NO_' + e.name)
            if e in (self._state | self.datafile._state):
                ret.append(e.name)
        return ret
    
    def _clearflag(self, flag):
        for f in StateFlag:
            if f == StateFlag.NONE:
                continue
            if f in flag and f in self._state:
                self._state &= ~f
        logging.debug(f"New state {self._state.value}: {self.state}")

    def _setflag(self, flag):
        if flag in self._state:
            return
        self._state = self._state | flag
        logging.debug(f"New state {self.state}")

    def _open(self):
        try:
            self.serial = Serial(self.config['serial_connection']['device'], 
                                 self.config['serial_connection']['baudrate'], 8, 'N', 1,
                                 timeout=self.config['serial_connection']['timeout'])
            self._setflag(StateFlag.SERIAL)
            logging.info(f"Serial port {self.config['serial_connection']['device']} opened.")

            if self.config['configure_upon_start']:
                self.config_instrument()
            if 'trigger_level' in self.config:
                self.set_trigger_levels()
        except Exception as e:
            raise Exception(e)
            self._clearflag(StateFlag.SERIAL)
            self.serial = None

    def _close(self):
        try:
            if self.serial is not None:
                logging.debug("Returning instrument to local mode")
                self._write("LOCL0")
                logging.info("Closing serial device...")
                self.serial.close()
                logging.info("Serial device closed.")
        except:
            logging.warning("Unable to properly close and release serial device.")
        finally:
            self.serial = None
            self._clearflag(StateFlag.SERIAL)

    def _write(self, command):
        #print(f"Writing {command}")
        self.serial.write(f"{command}\n".encode("ascii"))
        self.serial.flush()
        self.serial.reset_input_buffer()

    def _read(self):
        #ret.append(self.serial.readline().decode("ascii").strip())
        return self.serial.readline().decode("ascii").strip()

    def _query(self, command):
        self._write(command)
        return self._read()
    
    def config_instrument(self):
        logging.info("Configuring measurement instrument...")
        res = self._query("*IDN?")
        logging.debug(f"*IDN? > {res}")

        res = self._query("ERRS?")
        logging.debug(f"ERRS? > {res}")

        try:
            for cmd in self.config['instrument_configuration']:
                self._write(cmd['command'])
                logging.info(cmd['desc'])
                sleep(0.1)
                
            if 'trigger_level' in self.config:
                self._write(f"LEVL 1,{self.config['trigger_level']};LEVL 2,{self.config['trigger_level']}")
                logging.info(f"Trigger levels of channels A and B set to {self.config['trigger_level']} V.")

            logging.info("Measurement instrument configured.")
            if self.config['start_measurement_immediately']:
                self._write("STRT")
                logging.info("Measurement started.")
        except Exception as e:
            logging.error("Unable to configure SR620 instrument for measurement.", exc_info=e, stack_info=True)

    def set_trigger_levels(self):
        try:
            if 'trigger_level' in self.config:
                self._write(f"LEVL 1,{self.config['trigger_level']};LEVL 2,{self.config['trigger_level']}")
                logging.info(f"Trigger levels of channels A and B set to {self.config['trigger_level']} V.")
        except Exception as e:
            logging.error("Cannot set trigger level in SR620.set_trigger_levels().", exc_info=e, stack_info=True)


    def readvalue(self):
        try:
            self._write(self.config['read_value_commands'])
            data = self._read()
            self._read_error_count = 0
            return data
        except Exception as e:
            self._read_error_count += 1
            if (self._read_error_count >= 5):
                self._close()
            logging.exception("Unhandled exception in SR620.readvalue().", exc_info=e, stack_info=True)
    
    def writevalue(self, value):
        try:
            self.datafile.write(value)
        except Exception as e:
            logging.exception("Unhandled exception in SR620.writevalue().", exc_info=e, stack_info=True)
    
    def execute(self):
        while True:
            try:
                if StateFlag.SERIAL in self._state:
                    data = self.readvalue()
                    print(f"{datetime.utcnow().strftime('%Y-%M-%D (%j) %H:%m:%S')} {data}")
                    self.writevalue(data)
                else:
                    print(self.state)
                    sleep(3)                    
                    self._open()

            except KeyboardInterrupt as e:
                logging.info("Exiting after keyboard interrupt...")
                self._close()
                self.datafile.close()
                exit(1)

            except Exception as e:
                raise Exception(e)
                logging.exception("Unhandled exception in SR620.execute().", exc_info=e, stack_info=True)

def parseArguments():
    arp = argparse.ArgumentParser(prog='pps_compare.py', description='Script logs PPS delay measurement between A-B channels with SR620.', 
                                  epilog='You may disconnect the usb flash drive anytime. Just do not forget to plug it in again.')
    arp.add_argument('-c', '--config', dest='config', default='sr620-default.yml', help='Configuration file (default: ./sr620-default.yml)')
    ars = arp.add_argument_group('serial device', 'Serial port device configuration. Other settings are as SR620 defaults - 9600 8N1')
    ars.add_argument('-s', '--serial_device', dest='serial_device', help='Serial device file pointer. (default: /dev/ttyAMA0)')
    ars.add_argument('-t', '--timeout', dest='timeout', type=int, help='Serial device timeout (seconds). (default: 3)')
    arm = arp.add_argument_group('measurement', 'Configuration of the SR620 counter. Defaults are A-B time interval measurement, 1.5V trigger level, DC, 50 Ohms for both channels.')
    arm.add_argument('-i', '--config_instrument', action='store_true', dest='config_instrument', help='If set configures the measurement instrument upon opening the serial device. (default: False)')
    arm.add_argument('-l', '--trigger_level', type=float, dest='trigger_level', help='Trigger level for both A and B channel. (default: 1.5V)')
    arf = arp.add_argument_group('data files', 'Data file settings.')
    arf.add_argument('-d', '--data_path', dest='data_path', help='Folder for primary data file location. Must exists. (default: ./data/)')
    arf.add_argument('-p', '--prefix', dest='prefix', help='Data file prefix. (default: sr620-)')
    arf.add_argument('-u', '--usb_path', dest='usb_path', help='Folder for second data file location. (default: /media/usb/sr620)')
    arf.add_argument('-m', '--max_history', dest='max_history', type=int, help='Number of days after which data files will be deleted. (default: 999)')
    arf.add_argument('-y', '--max_sync', dest='max_sync', type=int, help='Number of days that will be sync to second data file location (e.g. usb drive) upon connection. (default: 32)')

    a = arp.parse_args()    
    ccfg = {}
    if a.config:
        ccfg['config'] = a.config
    if a.serial_device:
        ccfg['serial_connection'] = {'device': a.serial_device}
    if a.timeout:
        if 'serial_connection' in ccfg:
            ccfg['serial_connection']['timeout'] = a.timeout
        else:
            ccfg['serial_connection'] = {'timeout': a.timeout}
    if a.config_instrument:
        ccfg['configure_upon_start'] = a.config_instrument
    if a.trigger_level:
        ccfg['trigger_level'] = a.trigger_level
    if a.data_path:
        ccfg['primary_data_path'] = a.data_path
    if a.usb_path:
        ccfg['secondary_data_path'] = a.usb_path
    if a.prefix:
        ccfg['prefix'] = a.prefix
    if a.max_history:
        ccfg['max_history'] = a.max_history
    if a.max_sync:
        ccfg['max_sync'] = a.max_sync
    
    return ccfg

def merge_config_params():
    config = default_config
    ccfg = parseArguments()

    if ('config' in ccfg) and DualFileData._exists(ccfg['config']):
        # read yaml config
        with open(ccfg['config'], 'r') as f:
            ycfg = yaml.load(f, Loader=yaml.FullLoader)
        config = config | ycfg
    config = config | ccfg

    config['primary_data_path'] = os.path.normpath(config['primary_data_path'])
    config['secondary_data_path'] = os.path.normpath(config['secondary_data_path'])
    return config


if __name__ == "__main__":
    logging.basicConfig(filename=logfile, encoding='utf-8', filemode='w', level=logging.DEBUG,
                            format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S [%j]')

    logging.getLogger().addHandler(logging.StreamHandler())
    config = merge_config_params()
    logging.info(f"Program started with arguments {config}")
    ddf = DualFileData(config['primary_data_path'], config['prefix'], config['max_history'], config['max_sync'], config['secondary_data_path'])
    sr = SR620(ddf, config)
    logging.debug("Starting sr620.execute() loop...")
    sr.execute()