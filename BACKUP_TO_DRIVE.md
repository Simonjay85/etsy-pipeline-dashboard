# Etsy backup to Google Drive

The backup target is [Etsy Automated Backups](https://drive.google.com/drive/folders/1cg5xsQ_3HIPEDASOco9MddHrm993DoCA).

`backup_etsy_to_drive.py daily` runs the daily snapshot. It includes the live
Daisy and Temply workbooks, `shops_config.json`, `product_source_map.json`,
`active_shop.txt`, and generated JSON snapshots/reports.

`backup_etsy_to_drive.py weekly` additionally includes `master_products/` and
the `daisyflowdigital/` and `templystudios/` shop trees. Every snapshot has a
SHA-256 `manifest.json`; the oldest snapshots are purged after 30 versions per
cadence.

The two launchd definitions are:

- `com.user.etsy-backup.daily.plist`: every day at 03:15.
- `com.user.etsy-backup.weekly.plist`: Sunday at 03:45.

Run `./install_etsy_backup_launchd.command` once on the Mac to register both
jobs. Logs are written under `output/backup/`.

The job refuses to upload an incomplete snapshot when macOS reports a source
file as `compressed,dataless` (an iCloud placeholder). Hydrate the file first,
then rerun the corresponding command.
