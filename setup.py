#  This file is part of OctoBot Market Making (https://github.com/Drakkar-Software/OctoBot-Market-Making)
#  Copyright (c) 2025 Drakkar-Software, All rights reserved.
#
#  OctoBot is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  OctoBot is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  General Public License for more details.
#
#  You should have received a copy of the GNU General Public
#  License along with OctoBot. If not, see <https://www.gnu.org/licenses/>.
from setuptools import find_packages
from setuptools import setup
from octobot_market_making import PROJECT_NAME, AUTHOR, VERSION

PACKAGES = find_packages(exclude=["tentacles*", "tests", ])

# long description from README file
with open('README.md', encoding='utf-8') as f:
    DESCRIPTION = f.read()


REQUIRED = open('requirements.txt').readlines()
REQUIRES_PYTHON = '>=3.10'

setup(
    name=PROJECT_NAME,
    version=VERSION,
    url='https://github.com/Drakkar-Software/OctoBot-Market-Making',
    license='GPL-3.0',
    author=AUTHOR,
    author_email='contact@drakkar.software',
    description='Cryptocurrencies market making bot',
    py_modules=['start'],
    packages=PACKAGES,
    package_data={
        "": ["config/*"],
    },
    long_description=DESCRIPTION,
    long_description_content_type='text/markdown',
    tests_require=["pytest"],
    test_suite="tests",
    zip_safe=False,
    install_requires=REQUIRED,
    python_requires=REQUIRES_PYTHON,
    entry_points={
        'console_scripts': [
            PROJECT_NAME.replace("-", "_") + ' = octobot_market_making.cli:main'
        ]
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Operating System :: OS Independent',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: POSIX',
        'Programming Language :: Python :: 3.10',
    ],
)
