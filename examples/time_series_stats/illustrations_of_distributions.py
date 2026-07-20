import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        # Illustrations of the time-series statistics helpers

        This notebook is **not** a Petri net — it plots the building blocks used by the
        `stats_executable_graph` example so the numbers behind that graph are easy to see:

        1. A noisy sine-wave time series, with the rolling-window boundaries marked.
        2. The exponentially decaying weights applied within one window.
        3. The resulting exponential moving average (EMA) laid over the raw series.

        The helpers come from the sibling `hypothetical_time_series` module.
        """
    )
    return


@app.cell
def _():
    from datetime import datetime, timedelta

    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np
    from numpy.random import SFC64, Generator

    # Sibling domain module, imported by bare name (on marimo's sys.path at runtime).
    from hypothetical_time_series import SeriesStatistics, SimulateData

    return (
        Generator,
        SFC64,
        SeriesStatistics,
        SimulateData,
        datetime,
        mdates,
        np,
        plt,
        timedelta,
    )


@app.cell
def _(Generator, SFC64, SimulateData, datetime):
    # A single reproducible noisy sine wave, reused by every plot below.
    start_time = datetime(2020, 1, 1, 0, 0, 0)
    end_time = datetime(2020, 1, 2, 0, 0, 0)

    rng = Generator(SFC64(123453))
    unix_times_ms, values = SimulateData.generate_sine_wave_with_noise(
        start=start_time,
        end=end_time,
        n=100,
        amplitude=10.0,
        shift=15.0,
        noise_std=0.1,
        rng=rng,
        period_ms=(17 * 60 * 60 * 1000),  # in milliseconds
    )
    measurement_times = [datetime.fromtimestamp(t / 1000) for t in unix_times_ms]

    DECAY_PARAMETER = 0.0002
    return (
        DECAY_PARAMETER,
        end_time,
        measurement_times,
        start_time,
        values,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""## 1. The series, with rolling-window boundaries""")
    return


@app.cell
def _(
    SeriesStatistics,
    end_time,
    mdates,
    measurement_times,
    plt,
    start_time,
    timedelta,
    values,
):
    _window_starts_and_ends = SeriesStatistics.rolling_window_starts_and_ends(
        start_time, end_time, window_size=timedelta(hours=4), step_size=timedelta(minutes=20)
    )
    _window_ends = tuple(zip(*_window_starts_and_ends))[1]

    _fig, _ax = plt.subplots(figsize=(10, 5))
    _ax.plot(measurement_times, values)
    _ax.vlines(
        _window_ends, ymin=min(values), ymax=max(values), colors="r", linestyles="dashed"
    )
    _ax.xaxis.set_major_locator(mdates.DayLocator())
    _ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    _fig.autofmt_xdate()
    _ax.set(title="Values over time (dashed = rolling-window ends)", xlabel="Date", ylabel="Value")
    _ax.grid(True)
    _fig
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""## 2. Exponentially decaying weights within one window""")
    return


@app.cell
def _(DECAY_PARAMETER, SeriesStatistics, datetime, mdates, measurement_times, np, plt):
    _start_of_window = datetime(2020, 1, 1, 5, 0, 10)
    _end_of_window = datetime(2020, 1, 1, 12, 0, 30)

    _times_in_window, _weights = SeriesStatistics.exponentially_decaying_weighting_in_time_window(
        time_points=measurement_times,
        start_of_window=_start_of_window,
        end_of_window=_end_of_window,
        decay_parameter=DECAY_PARAMETER,
    )
    # Zero weight outside the window.
    _all_weights = np.zeros(len(measurement_times))
    for _t, _w in zip(_times_in_window, _weights):
        _all_weights[measurement_times.index(_t)] = _w

    _fig, _ax = plt.subplots(figsize=(10, 5))
    _ax.plot(measurement_times, _all_weights)
    _ax.xaxis.set_major_locator(mdates.DayLocator())
    _ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    _fig.autofmt_xdate()
    _ax.set(title="Weight applied to each point in one window", xlabel="Date", ylabel="Weight")
    _ax.grid(True)
    _fig
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""## 3. The exponential moving average over the raw series""")
    return


@app.cell
def _(
    DECAY_PARAMETER,
    SeriesStatistics,
    end_time,
    mdates,
    measurement_times,
    plt,
    start_time,
    values,
):
    _window_ends, _ema = SeriesStatistics.exponential_moving_average(
        measurement_times=measurement_times,
        values=values,
        start_time=start_time,
        end_time=end_time,
        decay_parameter=DECAY_PARAMETER,
    )

    _fig, _ax = plt.subplots(figsize=(10, 5))
    _ax.plot(measurement_times, values, label="raw")
    _ax.plot(_window_ends, _ema, "-r", label="EMA")
    _ax.xaxis.set_major_locator(mdates.DayLocator())
    _ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    _fig.autofmt_xdate()
    _ax.set(title="Raw series and its exponential moving average", xlabel="Date", ylabel="Value")
    _ax.legend()
    _ax.grid(True)
    _fig
    return


if __name__ == "__main__":
    app.run()
