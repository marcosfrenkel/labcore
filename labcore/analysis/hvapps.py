from datetime import datetime
from pathlib import Path
from typing import Any, Union
from collections import OrderedDict

import asyncio
import nest_asyncio
nest_asyncio.apply()

import param
import panel as pn
from panel.widgets import RadioButtonGroup as RBG, MultiSelect, Select

import re

from ..data.datadict_storage import find_data, timestamp_from_path, datadict_from_hdf5
from ..data.datadict import (
    DataDict,
    dd2df,
    datadict_to_meshgrid,
    dd2xr,
)
from .hvplotting import Node, labeled_widget


class DataSelect(pn.viewable.Viewer):

    SYM = {
        'complete': '✅',
        'star': '😁',
        'bad': '😭',
        'trash': '❌',
    }
    DATAFILE = 'data.ddh5'

    selected_path = param.Parameter(None)
    search_term = param.Parameter(None)

    @staticmethod
    def date2label(date_tuple):
        return "-".join((str(k) for k in date_tuple))

    @staticmethod
    def label2date(label):
        return tuple(int(l) for l in label[:10].split("-"))

    @staticmethod
    def group_data(data_list):
        ret = {}
        for path, info in data_list.items():
            ts = timestamp_from_path(path)
            date = (ts.year, ts.month, ts.day)
            if date not in ret:
                ret[date] = {}
            ret[date][path] = info
        return ret

    def __panel__(self):
        return self.layout

    def __init__(self, data_root, size=15):
        super().__init__()

        self.size = size

        # this contains a dict with the structure:
        # { date (as tuple): 
        #    { path of the dataset folder : (list of subdirs, list of files) }
        # }
        self.data_sets = self.group_data(find_data(root=data_root))

        self.layout = pn.Column()

        # a search bar for data
        self.text_input = pn.widgets.TextInput(
            name='Search', 
            placeholder='Enter a search term here...'
        )
        self.layout.append(self.text_input)

        # Display the current search term
        self.typed_value = pn.widgets.StaticText(
            stylesheets=[selector_stylesheet], 
            css_classes=['ttlabel'],
        )
        self.layout.append(self.text_input_repeater)

        # two selectors for data selection
        self._group_select_widget = MultiSelect(
            name='Date', 
            size=self.size,
            width=200,
            stylesheets = [selector_stylesheet]
        )
        self._data_select_widget = Select(
            name='Data set', 
            size=self.size,
            width=800,
            stylesheets = [selector_stylesheet]
        )

        # a simple info panel about the selection
        self.lbl = pn.widgets.StaticText(
            stylesheets=[selector_stylesheet], 
            css_classes=['ttlabel'],
        )
        self.layout.append(pn.Row(self._group_select_widget, self.data_select, self.info_panel))

        opts = OrderedDict()
        for k in sorted(self.data_sets.keys())[::-1]:
            lbl = self.date2label(k) + f' [{len(self.data_sets[k])}]'
            opts[lbl] = k
        self._group_select_widget.options = opts

    @pn.depends("_group_select_widget.value")
    def data_select(self):
        opts = OrderedDict()

        # setup global variables for search function
        active_search = False
        r = re.compile(".*")
        if hasattr(self, "text_input"): 
            if self.text_input.value_input is not None and self.text_input.value_input != "":
                # Make the Regex expression for the searched string
                r = re.compile(".*" + str(self.text_input.value_input) + ".*")
                print("Filter Term: " + str(self.text_input.value_input))
                active_search = True

        for d in self._group_select_widget.value:
            for dset in sorted(self.data_sets[d].keys())[::-1]:
                if active_search and not r.match(str(dset) + " " + str(timestamp_from_path(dset))):
                    # If Active search and this term doesn't match it, don't show
                    continue
                (dirs, files) = self.data_sets[d][dset]
                ts = timestamp_from_path(dset)
                time = f"{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}"
                uuid = f"{dset.stem[18:26]}"
                name = f"{dset.stem[27:]}"
                lbl = f" {time} - {uuid} - {name} "
                for k in ['complete', 'star', 'trash']:
                    if f'__{k}__.tag' in files:
                        lbl += self.SYM[k]             
                opts[lbl] = dset

        self._data_select_widget.options = opts
        return self._data_select_widget

    @pn.depends("_data_select_widget.value")
    def info_panel(self):
        path = self._data_select_widget.value
        if isinstance(path, Path):
            path = path / self.DATAFILE
        self.lbl.value = f"Path: {path}"
        self.selected_path = path
        return self.lbl
    
    @pn.depends("text_input.value_input")
    def text_input_repeater(self):
        self.typed_value.value = f"Current Search: {self.text_input.value_input}"
        self.search_term = self.text_input.value_input
        return self.typed_value


selector_stylesheet = """
:host .bk-input {
    font-family: monospace;
}

:host(.ttlabel) .bk-clearfix {
    font-family: monospace;
}
"""


class LoaderNodeBase(Node):
    """A node that loads data.

    the panel of the node consists of UI options for loading and pre-processing.

    Each subclass must implement ``LoaderNodeBase.load_data``.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        """Constructor for ``LoaderNode``.

        Parameters
        ----------
        *args:
            passed to ``Node``.
        **kwargs:
            passed to ``Node``.
        """
        # to be able to watch, this needs to be defined before super().__init__
        self.refresh = pn.widgets.Select(
            name="Auto-refresh",
            options={
                'None': None,
                '2 s': 2,
                '5 s': 5,
                '10 s': 10,
                '1 min': 60,
                '10 min': 600,
            },
            value="None",
            width=80,
        )
        self.task = None

        super().__init__(*args, **kwargs)

        self.pre_process_opts = RBG(
            options=[None, "Average"],
            value="Average",
            name="Pre-processing",
            align="end",
        )
        self.pre_process_dim_input = pn.widgets.TextInput(
            value="repetition",
            name="Pre-process dim.",
            width=100,
            align="end",
        )
        self.grid_on_load_toggle = pn.widgets.Toggle(
            value=True, name="Auto-grid", align="end"
        )
        self.generate_button = pn.widgets.Button(
            name="Load data", align="end", button_type="primary"
        )
        self.generate_button.on_click(self.load_and_preprocess)
        self.info_label = pn.widgets.StaticText(name="Info", align="start")
        self.info_label.value = "No data loaded."


        self.layout = pn.Column(
            pn.Row(
                labeled_widget(self.pre_process_opts),
                self.pre_process_dim_input,
                self.grid_on_load_toggle,
                self.generate_button,
                self.refresh,
            ),
            self.display_info,
        )

    def load_and_preprocess(self, *events: param.parameterized.Event) -> None:
        """Call load data and perform pre-processing.

        Function is triggered by clicking the "Load data" button.
        """
        t0 = datetime.now()
        dd = self.load_data()  # this is simply a datadict now.

        # if there wasn't data selected, we can't process it
        if dd is None:
            return

        # this is the case for making a pandas DataFrame
        if not self.grid_on_load_toggle.value:
            data = self.split_complex(dd2df(dd))
            indep, dep = self.data_dims(data)

            if self.pre_process_dim_input.value in indep:
                if self.pre_process_opts.value == "Average":
                    data = self.mean(data, self.pre_process_dim_input.value)
                    indep.pop(indep.index(self.pre_process_dim_input.value))

        # when making gridded data, can do things slightly differently
        # TODO: what if gridding goes wrong?
        else:
            mdd = datadict_to_meshgrid(dd)

            if self.pre_process_dim_input.value in mdd.axes():
                if self.pre_process_opts.value == "Average":
                    mdd = mdd.mean(self.pre_process_dim_input.value)

            data = self.split_complex(dd2xr(mdd))
            indep, dep = self.data_dims(data)

        for dim in indep + dep:
            self.units_out[dim] = dd.get(dim, {}).get("unit", None)

        self.data_out = data
        t1 = datetime.now()
        self.info_label.value = f"Loaded data at {t1.strftime('%Y-%m-%d %H:%M:%S')} (in {(t1-t0).microseconds*1e-3:.0f} ms)."

    @pn.depends("info_label.value")
    def display_info(self):
        return self.info_label
    
    @pn.depends("refresh.value", watch=True)
    def on_refresh_changed(self):
        if self.refresh.value is None:
            self.task = None
        
        if self.refresh.value is not None:
            if self.task is None:
                self.task = asyncio.ensure_future(self.run_auto_refresh())

    async def run_auto_refresh(self):
        while self.refresh.value is not None:
            await asyncio.sleep(self.refresh.value)
            self.load_and_preprocess()
        return

    def load_data(self) -> DataDict:
        """Load data. Needs to be implemented by subclasses.

        Raises
        ------
        NotImplementedError
            if not implemented by subclass.
        """
        raise NotImplementedError


class DDH5LoaderNode(LoaderNodeBase):
    """A node that loads data from a specified file location.

    the panel of the node consists of UI options for loading and pre-processing.

    """

    file_path = param.Parameter(None)

    def __init__(self, path: Union[str, Path] = "", *args: Any, **kwargs: Any):
        """Constructor for ``LoaderNodePath``.

        Parameters
        ----------
        *args:
            passed to ``Node``.
        **kwargs:
            passed to ``Node``.
        """
        super().__init__(*args, **kwargs)
        self.file_path = path

    def load_data(self) -> DataDict:
        """
        Load data from the file location specified
        """
        # Check in case no data is selected
        if str(self.file_path) == "":
            self.info_label.value = "Please select data to load. If there is no data, trying running in a higher directory."
            return None

        return datadict_from_hdf5(self.file_path.absolute())