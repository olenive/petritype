import os
import pytest
from pytest import mark

from petritype.helpers.io.io_helper import IOHelper


TEMPLATE_01_STRING = """' something on the first line.

Start ()

Some Text
\t .Thing1 "{values_0}"
\t .Thing2 "{values_1}"  ' 0.01
Other Text

Finish

"""


class TestIOHelper:

    def test_string_from_file_returns_expected_string(self):
        file_path = os.path.join("tests", "data", "templates", "example_01.template")
        result: str = IOHelper.string_from_file(file_path)
        expected = TEMPLATE_01_STRING
        assert result == expected

    def test_safe_move_file_moves_a_file(self):
        starting_directory = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "before_moving"
        )
        destination_directory = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "after_moving"
        )
        starting_path = os.path.join(starting_directory, "dummy_file.txt")
        finishing_path = os.path.join(destination_directory, "moved_dummy_file.txt")
        IOHelper.remove_file_if_it_exists(starting_path)
        IOHelper.remove_file_if_it_exists(finishing_path)
        IOHelper.make_directory(starting_directory)
        assert IOHelper.directory_is_empty(starting_directory)
        IOHelper.string_to_file("some text", starting_path)
        IOHelper.make_directory(destination_directory)
        assert IOHelper.directory_is_empty(destination_directory)
        IOHelper.safe_move_file(starting_path, finishing_path)
        assert IOHelper.directory_is_empty(starting_directory)
        assert "some text" == IOHelper.string_from_file(finishing_path)

    def test_safe_move_file_raises_if_destination_path_already_exits(self):
        starting_directory = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "before_moving"
        )
        destination_directory = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "after_moving"
        )
        starting_path = os.path.join(starting_directory, "dummy_file.txt")
        finishing_path = os.path.join(destination_directory, "moved_dummy_file.txt")
        IOHelper.remove_file_if_it_exists(starting_path)
        IOHelper.remove_file_if_it_exists(finishing_path)
        assert IOHelper.directory_is_empty(starting_directory)
        IOHelper.string_to_file("some text", starting_path)
        IOHelper.string_to_file("some other text", finishing_path)
        with pytest.raises(FileExistsError):
            IOHelper.safe_move_file(starting_path, finishing_path)
        assert "some text" == IOHelper.string_from_file(starting_path)
        assert "some other text" == IOHelper.string_from_file(finishing_path)

    @mark.skip("Not written")
    def test_safte_move_file_creates_destination_directory_if_needed(self):
        pass

    def test_directory_is_empty_when_directory_does_not_exist_raises(self):
        path = os.path.join("this", "should", "not", "be", "a", "valid", "path")
        assert not os.path.exists(path)
        with pytest.raises(FileNotFoundError):
            IOHelper.directory_is_empty(path)

    def test_directory_is_empty_with_empty_directory(self):
        path = os.path.join("tests", "helpers", "io", "directory_for_testing_file_operations", "empty_directory")
        if not os.path.exists(path):  # Make the empty directory because git does not store empty directories.
            IOHelper.make_directory(path, verbose=True)
        assert IOHelper.directory_is_empty(path)

    def test_directory_is_empty_with_non_empty_directory(self):
        path = os.path.join("tests", "helpers", "io", "directory_for_testing_file_operations", "non_empty_directory")
        assert not IOHelper.directory_is_empty(path)

    def test_safe_move_file_given_a_directory(self):
        starting_directory = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "for_testing_moving_of_directories",
            "origin_directory"
        )
        destination_directory = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "for_testing_moving_of_directories",
            "destination_directory"
        )
        IOHelper.remove_directory(starting_directory)
        IOHelper.remove_directory(destination_directory)
        IOHelper.make_directory(os.path.join(starting_directory, "dir"))
        IOHelper.make_directory(destination_directory)
        starting_path = os.path.join(starting_directory, "dir")
        finishing_path = os.path.join(destination_directory, "dir")
        IOHelper.safe_move_file(starting_path, finishing_path)
        assert os.path.isdir(finishing_path)

    def test_safe_move_directory_contents(self):
        base_dir = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "for_testing_moving_of_directories",
            "safe_move_directory_contents"
        )
        origin_directory = os.path.join(base_dir, "origin")
        destination_directory = os.path.join(base_dir, "destination")
        file_path_1_origin = os.path.join(origin_directory, "file_1.txt")
        file_path_2_origin = os.path.join(origin_directory, "file_2.txt")
        file_path_3_origin = os.path.join(origin_directory, "file_3.txt")
        dir_path_1_origin = os.path.join(origin_directory, "dir_1")
        file_path_1_destination = os.path.join(destination_directory, "file_1.txt")
        file_path_2_destination = os.path.join(destination_directory, "file_2.txt")
        file_path_3_destination = os.path.join(destination_directory, "file_3.txt")
        dir_path_1_destination = os.path.join(destination_directory, "dir_1")
        # Ensure origin directory is empty then create files in it.
        IOHelper.remove_directory(origin_directory)
        IOHelper.make_directory(origin_directory)
        assert IOHelper.directory_is_empty(origin_directory)
        IOHelper.string_to_file("Contents of file 1.", file_path_1_origin)
        IOHelper.string_to_file("Contents of file 2.", file_path_2_origin)
        IOHelper.string_to_file("Contents of file 3.", file_path_3_origin)
        IOHelper.make_directory(dir_path_1_origin)
        # Ensure the destination directory is empty
        IOHelper.remove_directory(destination_directory)
        IOHelper.make_directory(destination_directory)
        assert IOHelper.directory_is_empty(destination_directory)
        # Move files and check that all is as it should be
        IOHelper.safe_move_directory_contents(origin_directory, destination_directory)
        assert IOHelper.directory_is_empty(origin_directory)
        assert "Contents of file 1." == IOHelper.string_from_file(file_path_1_destination)
        assert "Contents of file 2." == IOHelper.string_from_file(file_path_2_destination)
        assert "Contents of file 3." == IOHelper.string_from_file(file_path_3_destination)
        assert os.path.isdir(dir_path_1_destination)

    def test_string_to_file_for_appending_to_file(self):
        path_to_testing_file = os.path.join(
            "tests", "helpers", "io", "directory_for_testing_file_operations", "append_string_to_file.txt"
        )
        IOHelper.remove_file_if_it_exists(path_to_testing_file)
        IOHelper.string_to_file("first line\n", path_to_testing_file)
        assert "first line\n" == IOHelper.string_from_file(path_to_testing_file)
        IOHelper.string_to_file("second line\n", path_to_testing_file, append=True)
        assert "first line\nsecond line\n" == IOHelper.string_from_file(path_to_testing_file)
