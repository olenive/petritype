import os
import shutil
import pickle
from typing import Dict, Any


class IOHelper:

    def string_from_file(file_path: str) -> str:
        with open(file_path, 'r') as file:
            return file.read()

    def string_to_file(contents: str, file_path: str, append=False) -> None:
        mode = 'a' if append else 'w'
        with open(file_path, mode) as file:
            file.write(contents)

    def strings_to_files(files_to_strings: Dict[str, str], output_directory="", file_prefix="") -> None:
        for key, value in files_to_strings.items():
            output_path = os.path.join(output_directory, file_prefix + key)
            IOHelper.string_to_file(value, output_path)

    def remove_file_if_it_exists(file_path: str) -> None:
        try:
            os.remove(file_path)
        except OSError:
            pass

    def safe_move_file(source_path: str, destination_path: str, verbose=False) -> None:
        """Only move the file if the destination path does not already correspond to an existing file."""
        if os.path.isfile(destination_path):
            raise FileExistsError(f"Attempted to move {source_path} but destination {destination_path} already exists")
        else:
            if verbose:
                print(f"Moving {source_path} to {destination_path}")
            IOHelper.make_directory(os.path.dirname(destination_path), verbose=verbose)
            shutil.move(source_path, destination_path)

    def safe_move_directory_contents(source_directory: str, destination_directory: str) -> None:
        if not os.path.isdir(source_directory) or not os.path.isdir(destination_directory):
            raise OSError(
                "Expecting paths to two directories but one or both of these are not directorise:\n"
                + str(source_directory) + '\n' + str(destination_directory)
            )
        for item in os.listdir(source_directory):
            IOHelper.safe_move_file(
                os.path.join(source_directory, item),
                os.path.join(destination_directory, item),
            )

    def make_directory(path_to_directory: str, verbose=False) -> None:
        if not (os.path.exists(path_to_directory)):
            if verbose:
                print(f"Making directory: {path_to_directory}")
            os.makedirs(path_to_directory)

    def pickle(data: Any, file_path: str) -> None:
        with open(file_path, 'wb') as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    def unpickle(file_path: str) -> Any:
        with open(file_path, 'rb') as f:
            return pickle.load(f)

    def directory_is_empty(path: str) -> bool:
        if len(os.listdir(path)) == 0:
            return True
        else:
            return False

    def remove_directory(path: str, verbose=False) -> None:
        try:
            shutil.rmtree(path)
        except OSError as e:
            if verbose:
                print("Failed to remove directory: %s - %s." % (e.filename, e.strerror))
