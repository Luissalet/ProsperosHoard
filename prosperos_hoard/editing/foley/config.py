"""Where the forge keeps its caches: FOLEY_HOME, else a folder under the user's cache."""
import os

HOME = os.path.abspath(os.environ['FOLEY_HOME']) if os.environ.get('FOLEY_HOME') else os.path.join(os.path.expanduser('~'), '.cache', 'foley')
