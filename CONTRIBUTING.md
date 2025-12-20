# Contribute to OctoBot Market Making

Feel like OctoBot Market Making is missing a feature? We welcome your pull requests!

As OctoBot Market Making is a distribution of OctoBot, most of its code is located in the [OctoBot repository](https://github.com/Drakkar-Software/OctoBot). Therefore, the best way to contribute is to follow the [OctoBot contributing guide](https://github.com/Drakkar-Software/OctoBot/blob/master/CONTRIBUTING.md).

## Enabling the Market Making distribution on an OctoBot developer environment 
Once your [OctoBot developer environment](https://www.octobot.cloud/en/guides/octobot-developers-environment/setup-your-environment) is configured:
1. Start OctoBot at least once to initialize the config files and install the OctoBot tentacles and stop it.
2. Open your `user/config.json` file with a text editor and change `"distribution:" "default"` to `"distribution:" "market_making"`.
3. Your OctoBot will now start using the Market Making distribution.
