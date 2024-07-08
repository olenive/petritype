from datetime import datetime, timedelta
from typing import Sequence
import numpy as np
from numpy.random import Generator


class SimulateData:

    def sample_uniform_unix_timestamps(start: datetime, end: datetime, n: int, rng: Generator) -> np.ndarray:
        if start > end:
            raise ValueError("The start timestamp must be smaller than the end timestamp.")
        start_timestamp = start.timestamp() * 1000  # Convert to milliseconds
        end_timestamp = end.timestamp() * 1000  # Convert to milliseconds
        return np.sort(rng.uniform(low=start_timestamp, high=end_timestamp, size=n))

    def sine_wave(
        x: np.ndarray,
        period_ms=(7 * 60 * 60 * 1000),  # 7 hours in milliseconds
    ) -> np.ndarray:
        """Generates a sine wave value for a given x in milliseconds with a period of 7 hours by default."""
        return np.sin(x * 2 * np.pi / period_ms)

    def add_noise(y: np.ndarray, noise_std: float, rng: Generator) -> np.ndarray:
        """Adds Gaussian noise to the input signal y with a specified standard deviation."""
        return y + rng.normal(scale=noise_std, size=len(y))

    def generate_sine_wave_with_noise(
        start: datetime,
        end: datetime,
        n: int,
        amplitude: float,
        shift: float,
        noise_std: float,
        rng: Generator,
        period_ms=(7 * 60 * 60 * 1000),
    ) -> tuple[np.ndarray, np.ndarray]:
        timestamps = SimulateData.sample_uniform_unix_timestamps(start, end, n, rng)
        sine_wave_values = SimulateData.sine_wave(timestamps, period_ms=period_ms) * amplitude + shift
        noisy_values = SimulateData.add_noise(sine_wave_values, noise_std * amplitude, rng)
        return timestamps, noisy_values


class SeriesStatistics:

    def exponentially_decaying_weighting_in_time_window(
        time_points: Sequence[datetime],
        start_of_window: datetime,
        end_of_window: datetime,
        decay_parameter: float,
    ) -> tuple[Sequence[datetime], np.ndarray]:
        """Compute weights for each time point in the time window.

        Calculate the decay parameter such that the weight is near the minimum weight at the start of the window
        and at the maximum weight at the end of the window.
        """
        time_points_in_window = [t for t in time_points if start_of_window <= t <= end_of_window]
        distances_from_end_of_window = np.array([end_of_window.timestamp() - t.timestamp() for t in time_points_in_window])
        weights = np.exp(-distances_from_end_of_window * decay_parameter)
        return time_points_in_window, weights

    def exponentially_decaying_mean_weighted_values_in_time_window(
        time_points: Sequence[datetime],
        values: np.ndarray,
        start_of_window: datetime,
        end_of_window: datetime,
        decay_parameter: float,
    ) -> tuple[Sequence[datetime], float]:
        """Compute weighted values for each time point in the time window."""
        if len(time_points) != len(values):
            raise ValueError("time_points and values must have the same length")
        time_points_in_window, weights = SeriesStatistics.exponentially_decaying_weighting_in_time_window(
            time_points, start_of_window, end_of_window, decay_parameter
        )
        values_at_time_points_in_window = values[np.isin(time_points, time_points_in_window)]
        weighted_average = np.average(values_at_time_points_in_window, weights=weights)
        return time_points_in_window, weighted_average

    def rolling_window_ends(
        start_time: datetime, end_time: datetime, window_size: timedelta, step_size: timedelta
    ) -> Sequence[datetime]:
        window_end = start_time + window_size
        out = []
        while window_end < end_time:
            out.append(window_end)
            window_end += step_size
        return out

    def rolling_window_starts_and_ends(
        start_time: datetime, end_time: datetime, window_size: timedelta, step_size: timedelta
    ) -> Sequence[tuple[datetime, datetime]]:
        window_end = start_time + window_size
        out = []
        while window_end < end_time:
            out.append((window_end - window_size, window_end))
            window_end += step_size
        return out

    def exponential_moving_average(
        measurement_times: Sequence[datetime],
        values: Sequence[float],
        start_time: datetime,
        end_time: datetime,
        decay_parameter: float,
    ) -> tuple[Sequence[datetime], Sequence[float]]:
        window_starts_and_ends = SeriesStatistics.rolling_window_starts_and_ends(
            start_time, end_time, window_size=timedelta(hours=4), step_size=timedelta(minutes=20)
        )
        mean_weighted_values = []
        window_ends = []
        for start, end in window_starts_and_ends:
            _, mean_weighted_value = SeriesStatistics.exponentially_decaying_mean_weighted_values_in_time_window(
                time_points=measurement_times,
                values=values,
                start_of_window=start,
                end_of_window=end,
                decay_parameter=decay_parameter,
            )
            mean_weighted_values.append(mean_weighted_value)
            window_ends.append(end)
        return window_ends, mean_weighted_values
