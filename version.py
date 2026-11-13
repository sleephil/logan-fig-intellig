"""Source-controlled release identity; deployment commit is supplied by Railway."""
import os
VERSION = '0.2.0'
PARSER_VERSION = '13f-2'
RELEASE_DATE = '2026-11-13'
def release():
    return {'version': VERSION, 'parser_version': PARSER_VERSION,
            'release_date': RELEASE_DATE, 'commit': os.getenv('RAILWAY_GIT_COMMIT_SHA', 'unavailable')}
