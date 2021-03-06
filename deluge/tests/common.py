import os
import sys
import tempfile

from twisted.internet import defer, protocol, reactor
from twisted.internet.defer import Deferred
from twisted.internet.error import CannotListenError

import deluge.configmanager
import deluge.core.preferencesmanager
import deluge.log
from deluge.error import DelugeError
from deluge.ui.util import lang

deluge.log.setup_logger("none")


def disable_new_release_check():
    deluge.core.preferencesmanager.DEFAULT_PREFS["new_release_check"] = False


def set_tmp_config_dir():
    config_directory = tempfile.mkdtemp()
    deluge.configmanager.set_config_dir(config_directory)
    return config_directory


def add_watchdog(deferred, timeout=0.05, message=None):

    def callback(value):
        if not watchdog.called:
            watchdog.cancel()
        if not deferred.called:
            if message:
                print message
            deferred.cancel()
        return value

    deferred.addBoth(callback)
    watchdog = reactor.callLater(timeout, defer.timeout, deferred)


def rpath(*args):
    return os.path.join(os.path.dirname(__file__), *args)

# Initialize gettext
lang.setup_translations()


class ProcessOutputHandler(protocol.ProcessProtocol):

    def __init__(self, callbacks, script, logfile=None, print_stderr=True):
        self.callbacks = callbacks
        self.script = script
        self.log_output = ""
        self.stderr_out = ""
        self.logfile = logfile
        self.print_stderr = print_stderr
        self.quit_d = None
        self.killed = False

    def connectionMade(self):  # NOQA
        self.transport.write(self.script)
        self.transport.closeStdin()

    def outConnectionLost(self):  # NOQA
        if not self.logfile:
            return
        with open(self.logfile, 'w') as f:
            f.write(self.log_output)

    def kill(self):
        if self.killed:
            return
        self.killed = True
        self.quit_d = Deferred()
        self.transport.signalProcess('INT')
        return self.quit_d

    def processEnded(self, status):  # NOQA
        self.transport.loseConnection()
        if self.quit_d is None:
            return
        if status.value.exitCode == 0:
            self.quit_d.callback(True)
        else:
            self.quit_d.errback(status)

    def check_callbacks(self, data, cb_type="stdout"):
        ret = False
        for c in self.callbacks:
            if cb_type not in c["types"] or c["deferred"].called:
                continue
            for trigger in c["triggers"]:
                if trigger["expr"] in data:
                    ret = True
                    if "cb" in trigger:
                        trigger["cb"](self, c["deferred"], data, self.log_output)
                    elif "value" not in trigger:
                        raise Exception("Trigger must specify either 'cb' or 'value'")
                    else:
                        val = trigger["value"](self, data, self.log_output)
                        if trigger.get("type", "callback") == "errback":
                            c["deferred"].errback(val)
                        else:
                            c["deferred"].callback(val)
        return ret

    def outReceived(self, data):  # NOQA
        """Process output from stdout"""
        self.log_output += data
        if self.check_callbacks(data):
            pass
        elif '[ERROR' in data:
            print data,

    def errReceived(self, data):  # NOQA
        """Process output from stderr"""
        self.log_output += data
        self.stderr_out += data
        self.check_callbacks(data, cb_type="stderr")
        if not self.print_stderr:
            return
        data = "\n%s" % data.strip()
        prefixed = data.replace("\n", "\nSTDERR: ")
        print "\n%s" % prefixed


def start_core(listen_port=58846, logfile=None, timeout=10, timeout_msg=None,
               custom_script="", print_stderr=True, extra_callbacks=None):
    config_directory = set_tmp_config_dir()
    daemon_script = """
import sys
import deluge.core.daemon_entry

sys.argv.extend(['-d', '-c', '%s', '-L', 'info', '-p', '%d'])

try:
    daemon = deluge.core.daemon_entry.start_daemon(skip_start=True)
    %s
    daemon.start()
except:
    import traceback
    sys.stderr.write("Exception raised:\\n %%s" %% traceback.format_exc())
""" % (config_directory, listen_port, custom_script)
    callbacks = []
    default_core_cb = {"deferred": Deferred(), "types": "stdout"}
    if timeout:
        default_core_cb["timeout"] = timeout

    # Specify the triggers for daemon log output
    default_core_cb["triggers"] = [
        {"expr": "Finished loading ", "value": lambda reader, data, data_all: reader},
        {"expr": "Couldn't listen on localhost:%d" % (listen_port), "type": "errback",  # Error from libtorrent
         "value": lambda reader, data, data_all: CannotListenError("localhost", listen_port,
                                                                   "Could not start deluge test client!\n%s" % data)},
        {"expr": "Traceback", "type": "errback",
         "value": lambda reader, data, data_all: DelugeError("Traceback found when starting daemon:\n%s" % data)}
    ]

    callbacks.append(default_core_cb)
    if extra_callbacks:
        callbacks.extend(extra_callbacks)

    process_protocol = start_process(daemon_script, callbacks, logfile, print_stderr)
    return default_core_cb["deferred"], process_protocol


def start_process(script, callbacks, logfile=None, print_stderr=True):
    """
    Starts an external python process which executes the given script.

    Args:
        script (str): The content of the script to execute
        callbacks (list): list of dictionaries specifying callbacks

        logfile (str): Optional logfile to write the output from the process
        print_stderr (bool): If the output from the process' stderr should be printed to stdout

    Returns:
        ProcessOutputHandler: The handler for the process's output

    Each entry in the callbacks list is a dictionary with the following keys:
      * "deferred": The deferred to be called when matched
      * "types": The output this callback should be matched against.
                 Possible values: ["stdout", "stderr"]
      * "timeout" (optional): A timeout in seconds for the deferred
      * "triggers": A list of dictionaries, each specifying specifying a trigger:
        * "expr": A string to match against the log output
        * "value": A function to produce the result to be passed to the callback
        * "type" (optional): A string that specifies wether to trigger a regular callback or errback.

    """
    cwd = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    process_protocol = ProcessOutputHandler(callbacks, script, logfile, print_stderr)

    # Add timeouts to deferreds
    for c in callbacks:
        if "timeout" in c:
            add_watchdog(c["deferred"], timeout=c["timeout"], message=c.get("timeout_msg", None))

    reactor.spawnProcess(process_protocol, sys.executable, args=[sys.executable], path=cwd)
    return process_protocol
