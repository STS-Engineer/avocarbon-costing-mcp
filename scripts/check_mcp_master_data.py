def main():
    print("This script has been replaced to avoid confusing DATABASE_URL with MCP/KPI_DB_Final.")
    print("Use scripts/check_backend_database_tables.py for avocarbon_costing / DATABASE_URL.")
    print("Use scripts/check_data_source_status.py to see whether KPI_DB_FINAL_URL is configured.")
    print("MCP 21 06 26 targets KPI_DB_Final, not DATABASE_URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
