import subprocess
import sys
import time

from prosperos_hoard.editing import proctree
from prosperos_hoard.editing.hoard_link import proc as hlproc

PARENT = ("import subprocess,sys,time;"
          "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
          "print(c.pid,flush=True);time.sleep(60)")


def test_kill_tree_stops_the_process_and_its_children_without_waiting_on_external_tools():
    process = subprocess.Popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    child = int(process.stdout.readline())
    assert hlproc.pid_alive(child)
    started = time.monotonic()
    assert proctree.kill_tree(process, grace_s=1.0)
    assert time.monotonic() - started < 2
    deadline = time.monotonic() + 5
    while hlproc.pid_alive(child) and time.monotonic() < deadline:
        time.sleep(.05)
    assert process.poll() is not None and not hlproc.pid_alive(child)
    process.stdout.close()


def test_kill_tree_of_an_exited_process_is_a_no_op():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert proctree.kill_tree(process)
