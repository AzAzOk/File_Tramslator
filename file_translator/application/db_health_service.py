"""Database health service - checks MySQL table availability."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class TableStatus:
    """Status of a single database table."""
    
    def __init__(self, name: str):
        self.name = name
        self.available = False
        self.error: str | None = None
        self.row_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "table": self.name,
            "available": self.available,
            "errors": [self.error] if self.error else [],
        }
        if self.row_count > 0 or (not self.available and not self.error):
            result["row_count"] = self.row_count
        return result


class DbHealthService:
    REQUIRED_TABLES = ["glossary"]
    OPTIONAL_TABLES = ["jobs", "journal"]
    
    def __init__(self):
        self._connect_args: dict[str, Any] = {
            "host": os.environ.get("GLOSSARY_DB_HOST", "dbserver"),
            "port": int(os.environ.get("GLOSSARY_DB_PORT", "3306")),
            "user": os.environ.get("GLOSSARY_DB_USER", "glossary"),
            "password": os.environ.get("GLOSSARY_DB_PASSWORD", ""),
            "database": os.environ.get("GLOSSARY_DB_NAME", "glossary"),
        }


    @staticmethod
    def is_configured() -> bool:
        return all([
            os.environ.get('GLOSSARY_DB_HOST'),
            os.environ.get('GLOSSARY_DB_USER'),
            os.environ.get('GLOSSARY_DB_PASSWORD'),
            os.environ.get('GLOSSARY_DB_NAME'),
        ])

    async def check_all_tables(self) -> dict[str, Any]:
        import pymysql
        
        if not self.is_configured():
            return {
                'available': False,
                'reason': 'MySQL not configured (GLOSSARY_DB_* env vars missing)',
                'tables': [],
            }
        
        import pymysql.cursors as cursors_module
        self._connect_args['cursorclass'] = cursors_module.DictCursor
        
        result: dict[str, Any] = {
            'available': False,
            'reason': '',
            'tables': [],
        }
        
        tables_to_check = list(self.REQUIRED_TABLES) + list(self.OPTIONAL_TABLES)
        statuses: list[TableStatus] = []
        
        try:
            conn = pymysql.connect(**self._connect_args)
            try:
                for table_name in tables_to_check:
                    status = TableStatus(table_name)
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s',
                                (self._connect_args['database'], table_name)
                            )
                            row_count_result = cur.fetchone()
                            
                            if not row_count_result or row_count_result[0] == 0:
                                status.error = 'Table ' + repr(table_name) + ' does not exist in database'
                                statuses.append(status)
                                continue
                            
                            cur.execute('SELECT COUNT(*) FROM ' + table_name)
                            count_row = cur.fetchone()
                            if count_row:
                                status.row_count = int(count_row[0]) or 0
                            status.available = True
                    except Exception as e:
                        status.error = 'Error checking table ' + repr(table_name) + ': ' + str(e)
                        logging.getLogger(__name__).warning('MySQL table check failed for %s: %s', table_name, e)
                    
                    statuses.append(status)
                
                required_statuses = [s for s in statuses if s.name in self.REQUIRED_TABLES]
                all_required_ok = all(s.available for s in required_statuses)
                
                result['available'] = all_required_ok
                
                if not all_required_ok:
                    missing = [s.name for s in required_statuses if not s.available]
                    result['reason'] = 'Required tables missing or inaccessible: ' + ', '.join(missing)
                else:
                    missing_optional = [s.name for s in statuses 
                                       if s.name in self.OPTIONAL_TABLES and not s.available]
                    if missing_optional:
                        result['reason'] = 'Optional tables missing: ' + ', '.join(missing_optional)
                
                result['tables'] = [s.to_dict() for s in statuses]
                
            finally:
                conn.close()
        except Exception as e:
            logging.getLogger(__name__).error('MySQL connection failed: %s', str(e))
            result['available'] = False
            result['reason'] = 'Cannot connect to MySQL database: ' + str(e)
            result['tables'] = []
        
        return result
    
    async def is_glossary_available(self) -> bool:
        health = await self.check_all_tables()
        for table in health.get('tables', []):
            if table.get('table') == 'glossary' and table.get('available'):
                return True
        return False if not health['available'] else len(health.get('tables', [])) > 0


_db_health_service: DbHealthService | None = None

def get_db_health_service() -> DbHealthService:
    global _db_health_service
    if _db_health_service is None:
        _db_health_service = DbHealthService()
    return _db_health_service
