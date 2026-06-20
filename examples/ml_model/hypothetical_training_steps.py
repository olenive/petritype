from dataclasses import dataclass
from typing import Any


@dataclass
class VisionModel:
    pass


@dataclass
class FineTunedVisionModel:
    pass


@dataclass
class EvaluationMetrics:
    pass


@dataclass
class AvailableData:
    pass


@dataclass
class TrainingData:
    pass


@dataclass
class AugmentedTrainingData:
    pass


@dataclass
class EvaluationData:
    pass


@dataclass
class TestData:
    pass


@dataclass
class DeploymentModel:
    pass


def train_validation_test_split(data: AvailableData) -> tuple[TrainingData, EvaluationData, TestData]:
    pass


def data_augmentation(data: TrainingData) -> AugmentedTrainingData:
    pass


def fine_tune(model: VisionModel, data: AugmentedTrainingData) -> FineTunedVisionModel:
    pass


def evaluate_model(model: FineTunedVisionModel, data: Any) -> EvaluationMetrics:
    # `data` is any held-out dataset (validation or test).
    pass


def select_another_model(
    model: FineTunedVisionModel, metrics: EvaluationMetrics
) -> VisionModel | FineTunedVisionModel:
    # Either propose a new VisionModel to try, or accept the current FineTunedVisionModel.
    pass


def package_model(model: FineTunedVisionModel) -> DeploymentModel:
    pass


def integration_testing(model: DeploymentModel, data: TestData) -> EvaluationMetrics:
    pass
