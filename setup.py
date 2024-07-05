from setuptools import setup, find_packages


setup(
    name='petritype',
    version='0.1.0',
    packages=find_packages(),
    install_requires=[
        "pydantic==2.7.1",
        "rustworkx==0.14.2",
        "setuptools==69.5.1",
    ],
    entry_points={
        'console_scripts': [
            # If you have scripts to expose, list them here
        ],
    },
)
