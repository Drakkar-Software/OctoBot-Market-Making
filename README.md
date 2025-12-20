# Open Source Crypto Market Making Strategy Software
[![PyPI](https://img.shields.io/pypi/v/octobot_market_making.svg?logo=pypi)](https://pypi.org/project/octobot_market_making)
[![Dockerhub](https://img.shields.io/docker/pulls/drakkarsoftware/octobot.svg?logo=docker)](https://hub.docker.com/r/drakkarsoftware/octobot)
[![OctoBot-Market-Making-CI](https://github.com/Drakkar-Software/OctoBot-Market-Making/workflows/OctoBot-Market-Making-CI/badge.svg)](https://github.com/Drakkar-Software/OctoBot-market-making/actions)
[![Telegram](https://img.shields.io/badge/Telegram-grey.svg?logo=telegram)](https://t.me/OctoBot_Project)
[![Twitter](https://img.shields.io/twitter/follow/DrakkarsOctobot.svg?label=twitter&style=social)](https://x.com/DrakkarsOctoBot)
[![YouTube](https://img.shields.io/youtube/channel/views/UC2YAaBeWY8y_Olqs79b_X8A?label=youtube&style=social)](https://www.youtube.com/@octobot1134)


OctoBot Market Making is a market-making strategy automation bot. The open source software is designed to help crypto projects improve their crypto market liquidity.

<p align="middle">
  <img alt='octobot market making preview' src='https://raw.githubusercontent.com/Drakkar-Software/OctoBot-Market-Making/master/docs/octobot-market-making-preview.gif'/>
<p>

OctoBot Market Making is a market making bot that:
- Supports [more than 15 exchanges](#automated-market-making-on-more-than-15-exchanges)
- Uses a transparent open source [market making strategy](#the-octobot-market-making-strategy) automation algorithm
- Helps [crypto projects and individuals](#a-free-market-making-bot-for-crypto-projects-and-individuals) deploying simple and efficient market making strategies

## The OctoBot Market Making Strategy

The market making strategy automated by this bot is designed to improve the liquidity of any crypto spot market towards the following goals:
- **Increasing a crypto market's attractiveness** by reducing bid-ask spread and premiums
- **Enabling large trades** by increasing available funds in the order book
- **Ensuring a fair market price** by reducing arbitrage opportunities

<p align="middle">
  <img alt='octobot market making strategy configuration' src='https://raw.githubusercontent.com/Drakkar-Software/OctoBot-Market-Making/master/docs/octobot-market-making-strategy-configuration.png' width="630px"/>
</p>

To reach this goal, OctoBot Market Making creates and keeps up-to-date a set of buy and sell orders in the market's order book.  
- Shape and price of your market making orders can be configured according to your goals.
- Size of your strategy orders scales with your budget.
- Computation of a fair market price is configurable using the liquid exchange for your market.

### Order book design

Configure your ideal exchange liquidity by specifying how many bids and asks must be included in your strategy and the price range your orders should cover.

### Order book maintenance

The algorithm automatically replaces filled orders and adapts the order book according to the current price of your trading pair.

### Arbitrage protection

OctoBot Market Making builds its order book according to a reference price for the pair it provides liquidity on. This reference price can be from the local exchange or from another exchange with more liquidity on this pair.

Using another exchange as reference price will synchronize your bot’s order book around the price of this pair on the reference exchange. As a result, the strategy will instantly cancel and replace any order that does not align with your reference exchange price, effectively preventing arbitrage opportunities when the reference exchange has a more up-to-date price.

### Paper trading

OctoBot Market Making comes with a built-in trading simulator which you can use to configure your strategy and test it before connecting your bot to a real exchange account

<p align="middle">
  <img alt='octobot market making paper trading configuration' src='https://raw.githubusercontent.com/Drakkar-Software/OctoBot-Market-Making/master/docs/octobot-market-making-paper-trading-configuration.png' width="630px"/>
</p>

## A free market making bot for crypto projects and individuals

<p align="middle">
  <img alt='octobot market making dashboard with buy and sell orders' src='https://raw.githubusercontent.com/Drakkar-Software/OctoBot-Market-Making/master/docs/octobot-market-making-dashboard-with-buy-and-sell-orders.png' width="630px"/>
</p>

### Market making for crypto projects

Using an open source market making software lets crypto projects:
- **Generate liquidity** for their token on exchanges in a simple and free way.
- **Protect the token**: As OctoBot Market Making is fully transparent, users always know what the strategy can and can't do with their token: it's all open source.
- **Stay in control**: OctoBot Market Making is [self-custody](https://www.octobot.cloud/en/blog/how-to-use-a-self-custody-crypto-trading-bot?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=self_custody_trading_bot), there is no third party to trust with the project's coins.

### Market making for individuals

Individuals can also use a market making bot to:
- Profit from stable markets: extract profits from local ups and downs, in a grid-like fashion.
- Increase an exchange account trading volume to access higher fee tiers and reduce exchange fees.
- Earn exchanges liquidity provider rewards by participating in liquidity providing campaigns
- Farm volume-based DEX points for crypto airdrops, like the Hyperliquid HYPE airdrops.

## Automated market making on more than 15 exchanges

OctoBot Market Making supports all the [15+ OctoBot supported exchanges](https://www.octobot.cloud/en/guides/exchanges?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=exchanges_full_list), which includes [Binance](https://www.octobot.cloud/en/guides/octobot-partner-exchanges/binance?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=binance), [MEXC](https://www.octobot.cloud/en/guides/octobot-partner-exchanges/mexc?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=mexc), [Bitmart](https://www.octobot.cloud/en/guides/octobot-partner-exchanges/bitmart?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=bitmart), [Hyperliquid](https://www.octobot.cloud/en/guides/octobot-supported-exchanges/hyperliquid?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=hyperliquid), [CoinEx](https://www.octobot.cloud/en/guides/octobot-partner-exchanges/coinex?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=coinex), [Kucoin](https://www.octobot.cloud/en/guides/octobot-partner-exchanges/kucoin?utm_source=github&utm_medium=dk&utm_campaign=regular_market_making_open_source_content&utm_content=kucoin) and many others.

<p align="middle">
  <img alt='list of octobot supported exchanges including binance coinbase hyperliquid mexc and more' src='https://raw.githubusercontent.com/Drakkar-Software/OctoBot/refs/heads/assets/list-of-octobot-supported-exchanges-including-binance-coinbase-hyperliquid-mexc-and-more.png' width="630px"/>
</p>

Note: [HollaEx-Powered](https://www.octobot.cloud/en/guides/octobot-partner-exchanges/hollaex/account-setup) exchanges are supported by the market making bot.

## Installation

### Using the OctoBot Market Making Docker image
You can install OctoBot Market Making using its [dedicated Docker image](https://hub.docker.com/r/drakkarsoftware/octobot), available under the `marketmaking` tag of the OctoBot image

Docker install in one line:
```shell
docker run -itd --name OctoBot-Market-Making -p 80:5001 -v $(pwd)/user:/octobot/user -v $(pwd)/tentacles:/octobot/tentacles -v $(pwd)/logs:/octobot/logs drakkarsoftware/octobot:marketmaking-stable
```

### Installing OctoBot Market Making using Python

```shell
git clone https://github.com/Drakkar-Software/OctoBot-Market-Making
cd OctoBot-Market-Making
python -m pip install -Ur requirements.txt
python start.py
```

## Advanced Market Making

OctoBot Market Making is the backbone of [OctoBot cloud Market Making](https://market-making.octobot.cloud/?utm_source=github&utm_medium=dk&utm_campaign=regular_open_source_content&utm_content=going_further_1), a self-service market making automation platform. 

If you enjoy OctoBot Market Making and wish to automate more complex market making strategies for:
- [More flexibility](https://market-making.octobot.cloud/en/guides/starting-your-market-making-bot?utm_source=github&utm_medium=dk&utm_campaign=regular_open_source_content&utm_content=more_flexibility) in your market making strategy configuration.
- [Better risk management](https://market-making.octobot.cloud/en/guides/configuring-and-protecting-your-market-making-funds?utm_source=github&utm_medium=dk&utm_campaign=regular_open_source_content&utm_content=better_risk_management) to protect your funds from unexpected market events.
- [Dynamic fair price computation](https://market-making.octobot.cloud/en/guides/using-formulas-to-configure-your-market-making-reference-price?utm_source=github&utm_medium=dk&utm_campaign=regular_open_source_content&utm_content=dynamic_price_computation) to adapt your strategy to every situation.

... then [OctoBot cloud Market Making](https://market-making.octobot.cloud/?utm_source=github&utm_medium=dk&utm_campaign=regular_open_source_content&utm_content=going_further_2) is the right platform for you.

## How to contribute to OctoBot Market Making

Would you like to add or improve something in OctoBot Market Making or its documentation? We welcome your pull requests!  
Please have a look at our [contributing guide](CONTRIBUTING.md) to read our guidelines.

## OctoBot Market Making is based on OctoBot

OctoBot Market Making is a distribution of [OctoBot](https://github.com/Drakkar-Software/OctoBot), a free open source crypto trading robot, which is being actively developed since 2018.

It leverages the automated trading strategy engine of OctoBot to create and maintain an order book according to your strategy configuration. 

## Hardware requirements  
- CPU : 1 Core / 1GHz  
- RAM : 250 MB  
- Disk : 1 GB

## Disclaimer
Do not risk money which you are afraid to lose. USE THE SOFTWARE AT YOUR OWN RISK. THE AUTHORS 
AND ALL AFFILIATES ASSUME NO RESPONSIBILITY FOR YOUR TRADING RESULTS. 

Always start by running a trading bot in simulation mode and do not engage money
before you understand how it works and what profit/loss you should expect.

Please feel free to read the source code and understand the mechanism of this bot.

## License
GNU General Public License v3.0 or later.

See [GPL-3.0 LICENSE](https://github.com/Drakkar-Software/OctoBot-Market-Making/blob/master/LICENSE) to see the full text.


## Give a boost to OctoBot Market Making
Do you like what we are building with OctoBot Market Making? Consider giving us a star ⭐ to boost the project's visibility! 
