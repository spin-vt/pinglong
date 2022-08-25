from setuptools import setup

# Load README
with open("README.md") as f:
    README = f.read()

setup(
    name='pinglong',
    version=0.2,
    description='Pinglong: A tool for logging pings to end hosts for BDC',
    author='Shaddi Hasan',
    author_email="shaddi@vt.edu",
    install_requires=[
        "icmplib==3.0.3",
    ],
    py_modules=['pinglong'],
    scripts=["pinglong"],
)
