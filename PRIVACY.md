# RIA — Privacy Policy

Effective date: 2026-09-01
Contact: giwon.bae77@gmail.com

## 1. What RIA is

RIA is a personal, non-commercial research tool that runs locally on the developer's own computer. It gathers publicly available evidence for the developer's own product research and stores it in a local database. It is operated by a single individual (Giwon Bae) and is not offered to the public as a service. There are no user accounts, no sign-ups, and no third-party users.

## 2. Data RIA collects

RIA reads public content through official platform APIs (for example the Threads API and the Reddit Data API) and public data sources (national statistics, corporate disclosures, World Bank indicators, Hacker News).

For each item it stores: the platform's post identifier, permalink, title or text excerpt, public engagement counts (for example like or reply counts), media type, creation time, and the time of observation.

RIA does not collect private messages, non-public content, contact lists, passwords, payment information, or precise location.

Access tokens for the developer's own platform accounts are stored only in a local `.env` file on the developer's computer. They are never committed to source control and are transmitted only to the platform that issued them.

## 3. How data is used

Collected data is used only to build evidence packages for the developer's private product research. It is not used for advertising, profiling, or resale, and it is never used to train or fine-tune any AI model.

## 4. Storage and retention

All data is stored in a local SQLite database on the developer's computer. Nothing is uploaded to any server operated by RIA. Each data source is registered with its own retention rule, and a retention job deletes or refreshes stored records according to those rules and to the platform's own policy (platform-supplied data is refreshed or removed within the window the platform requires).

## 5. Sharing

RIA does not share, sell, publish, syndicate, or redistribute collected data to any third party.

## 6. Personal data

RIA collects public posts, not people. Usernames and other personal identifiers are dropped unless strictly required for attribution, and RIA never attempts to identify individuals or link accounts across platforms.

## 7. Data deletion

If you believe RIA has stored content that belongs to you and you want it removed, email giwon.bae77@gmail.com with a link to the post. The record will be deleted from the local database within 7 days and you will receive a confirmation. Platform access can also be revoked at any time from the platform's own app or account settings; RIA does not retain platform access tokens after revocation.

## 8. Changes

Changes to this policy are recorded in the version history of this file in the public repository at https://github.com/giwon-bae/RIA.
