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
import mock

import octobot.cli
import octobot_market_making.cli
import pathlib


def test_start_octobot_with_market_making_config():
    with mock.patch.object(octobot.cli, "main") as octobot_main_mock:
        octobot_market_making.cli.main()
        octobot_main_mock.assert_called_once()
        assert isinstance(octobot_main_mock.mock_calls[0].args[0], list)
        repo_path = pathlib.Path(__file__).parent.parent.absolute()
        # ensure octobot cli can be imported and is called with octobot_market_making default config
        assert octobot_main_mock.mock_calls[0].kwargs["default_config_file"].replace("\\", "/") == str(
            repo_path.joinpath("octobot_market_making/config/default_config.json")
        ).replace("\\", "/")
