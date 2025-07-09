# ![PDFScrape Logo](logo.svg) Integration


[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square&logo=homeassistantcommunitystore)](https://hacs.xyz/)
![GitHub Release](https://img.shields.io/github/v/release/iluvdata/pdf_scrape)


A [HACS](https://www.hacs.xyz/) integration that scrapes text pdf files from the web (available via http: and https: only).  Using a combination of regex and [limited templates](https://www.home-assistant.io/docs/configuration/templating/#limited-templates) it creates sensors that are then updated using polling.

Configuration via Homeassistant UI.

## Requirements

Homeassistant core: ≥2025.6.0

Homeassistant frontend: dev

## Installation with HACS

The recommended way to install this is via HACS:



[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=custom_respository&owner=iluvdata&repository=pdf_scrape)

#### Semi-manual install

1. Click on HACS in the Homeassistant side bar
2. Click on the three dots in the upper right-hand corner and select "Custom repositories."
3. In the form enter:

    1. Respository: `iluvdata/pdf_scrape`
    2. Select "Integration" as "Type"

## Manual Installation

Copy the `pdf_scrape` directory to the `custom_components` directory of your Homeassistant Instance.

## Configuration

### Configure a PDF

Add the intergration to Home Assistant:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=pdf_scrape)

You should be prompted enter a name (optional), url (required), and the polling interval (mininum 30s).  Long intervals are recommended as pdf files tend to be static and you don't want to be blocked for too frequent of requests or overburden your system.

#### Configure additional PDFs

Click "Add Hub" in the integration's configuration screen.

### Configure a sensor

Go to the intergation's dashboard.

[![Open your Home Assistant instance and show an integration.](https://my.home-assistant.io/badges/integration.svg)](https://my.home-assistant.io/redirect/integration/?domain=pdf_scrape)

Click on the three dots to the right of your intergation's entry name.  On the menu click "+ Add Search Target" to start the configuration flow for the search target.  This should be intuitive.
