from sunshine.models import Committee, Candidate, Officer, Candidacy
import ftplib
from io import BytesIO
import os
from boto.s3.connection import S3Connection
from boto.s3.key import Key
from datetime import date
from hashlib import md5
import sqlalchemy as sa
import csv


class SunshineExtract(object):
    
    def __init__(self, 
                 download_path='downloads',
                 ftp_host=None,
                 ftp_path=None,
                 ftp_user=None,
                 ftp_pw=None,
                 aws_key=None,
                 aws_secret=None):
        
        self.ftp_host = ftp_host
        self.ftp_user = ftp_user
        self.ftp_pw = ftp_pw
        self.ftp_path = ftp_path

        self.aws_key = aws_key
        self.aws_secret = aws_secret
        
        self.bucket_name = 'il-elections'
        self.download_path = download_path
    
    def cacheOnS3(self):
        keys = []
        with ftplib.FTP(self.ftp_host) as ftp:
            ftp.login(self.ftp_user, self.ftp_pw)
            files = ftp.nlst(self.ftp_start)
            for f in files:
                print('working on %s' % f)
                fobj = BytesIO()
                fpath ='%s/%s' % (self.ftp_path, f)
                ftp.retrbinary('RETR %s' % fpath, fobj.write)
                
                fobj.seek(0)

                conn = S3Connection(self.aws_key, self.aws_secret)
                bucket = conn.get_bucket(self.bucket_name)
                
                k = Key(bucket)
                keyname = 'sunshine/%s_%s.%s' % (f, date.today().isoformat(), f.rsplit('.', 1)[-1])
                k.key = keyname
                k.set_contents_from_file(fobj)
                k.make_public()
                
                keys.append(keyname)
                
                bucket.copy_key('sunshine/%s_latest.txt' % (f), 
                                self.bucket_name,
                                keyname,
                                preserve_acl=True)
        return keys
    
    def getLatestFiles():
        latest_files = []
        conn = S3Connection(app_config.AWS_KEY, app_config.AWS_SECRET)
        bucket = conn.get_bucket('il-elections')
        for key in bucket.list(prefix='sunshine'):
            if key.name.endswith('latest.txt'):
                fpath = 'downloads/%s' % key.name.replace('sunshine/', '')
                latest_files.append((fpath, key.etag))
        return latest_files

class SunshineTransformLoad(object):

    def __init__(self, 
                 engine,
                 metadata,
                 chunk_size=10000):

        
        self.engine = engine
        self.metadata = metadata
        
        self.chunk_size = chunk_size

        self.initializeDB()

        
        self.file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                                      'downloads', 
                                      self.filename)

    def initializeDB(self):
        enum = ''' 
            CREATE TYPE committee_position AS ENUM (
              'support', 
              'oppose'
            )
        '''
        conn = self.engine.connect()
        trans = conn.begin()
        
        try:
            conn.execute(enum)
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
        
        self.metadata.create_all(bind=self.engine)
    
    def createTempTable(self):
        create = ''' 
            CREATE TABLE temp_{0} AS
              SELECT * FROM {0} LIMIT 1
            WITH NO DATA
        '''.format(self.table_name)
        with self.engine.begin() as conn:
            conn.execute('DROP TABLE IF EXISTS temp_{0}'.format(self.table_name))
            conn.execute(create)
    
    @property
    def upsert(self):
        field_format = '{1} = subq.{1}'
        
        update_fields = [field_format.format(self.table_name,f) \
                             for f in self.header]
        
        return ''' 
            WITH upsert AS (
              UPDATE {0} SET 
                {1}
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              WHERE {0}.id = subq.id
              RETURNING *
            )
            INSERT INTO {0} 
              SELECT * FROM temp_{0}
            WHERE NOT EXISTS (SELECT * FROM upsert)
        '''.format(self.table_name, 
                   ','.join(update_fields))

    def update(self):

        with self.engine.begin() as conn:
            conn.execute(sa.text(self.upsert))

        with self.engine.begin() as conn:
            conn.execute('DROP TABLE temp_{0}'.format(self.table_name))

    def transform(self):
        raise NotImplementedError

    def load(self):
        self.createTempTable()
        
        insert = ''' 
            INSERT INTO temp_{0} ({1}) VALUES ({2})
        '''.format(self.table_name,
                   ','.join(self.header),
                   ','.join([':%s' % h for h in self.header]))

        rows = []
        i = 1
        for row in self.transform():
            rows.append(row)
            if len(rows) % self.chunk_size is 0:
                
                with self.engine.begin() as conn:
                    conn.execute(sa.text(insert), *rows)
                
                print('Loaded %s %s' % ((i * self.chunk_size), self.table_name))
                i += 1
                rows = []
        if rows:
            with self.engine.begin() as conn:
                conn.execute(sa.text(insert), *rows)
    
class SunshineCommittees(SunshineTransformLoad):
    
    table_name = 'committees'
    header = Committee.__table__.columns.keys()
    filename = 'Committees.txt_latest.txt'
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None

                    # Replace status value
                    if row[14] != 'A':
                        row[14] = False
                    else:
                        row[14] = True

                    # Replace position values
                    for idx in [23, 24]:
                        if row[idx] == 'O':
                            row[idx] = 'oppose'
                        elif row[idx] == 'S':
                            row[idx] = 'support'
                        else:
                            row[idx] = None
                    
                    yield dict(zip(self.header, row))
    

class SunshineCandidates(SunshineTransformLoad):
    
    table_name = 'candidates'
    header = [f for f in Candidate.__table__.columns.keys() \
              if f not in ['date_added', 'last_update', 'ocd_id']]
    filename = 'Candidates.txt_latest.txt'
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None

                    yield dict(zip(self.header, row))
    
    @property
    def upsert(self):
        field_format = '{1} = subq.{1}'
        
        update_fields = [field_format.format(self.table_name,f) \
                             for f in self.header]
        
        return ''' 
            WITH upsert AS (
              UPDATE {0} SET 
                {1},
                last_update = NOW()
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              WHERE {0}.id = subq.id
              RETURNING *
            )
            INSERT INTO {0} ({2})
              SELECT 
                {3},
                NOW() AS last_update,
                NOW() AS date_added
              FROM temp_{0}
            WHERE NOT EXISTS (SELECT * FROM upsert)
        '''.format(self.table_name, 
                   ','.join(update_fields),
                   ','.join(self.header + ['last_update', 'date_added']),
                   ','.join(self.header))

class SunshineOfficers(SunshineTransformLoad):
    table_name = 'officers'
    header = Officer.__table__.columns.keys()
    filename = 'Officers.txt_latest.txt'
    current = True

    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        
                        if not cell:
                            row[idx] = None
                    
                    # Add empty committee_id
                    row.insert(1, None)

                    # Add empty resign date
                    row.insert(11, None)

                    # Add current flag
                    row.append(self.current)
                    
                    yield dict(zip(self.header, row))
    
class SunshinePrevOfficers(SunshineTransformLoad):
    table_name = 'officers'
    header = Officer.__table__.columns.keys()
    filename = 'PrevOfficers.txt_latest.txt'
    current = False
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None
                    
                    # Add empty phone
                    row.insert(10, None)

                    # Add current flag
                    row.append(self.current)

                    yield dict(zip(self.header, row))

class SunshineCandidacy(SunshineTransformLoad):
    table_name = 'candidacies'
    header = Candidacy.__table__.columns.keys()
    filename = 'CanElections.txt_latest.txt'
    
    election_types = {
        'CE': 'Consolidated Election',
        'GP': 'General Primary',
        'GE': 'General Election',
        'CP': 'Consolidated Primary',
        'NE': None,
        'SE': 'Special Election'
    }

    race_types = {
        'Inc': 'incumbent',
        'Open': 'open seat',
        'Chal': 'challenger',
        'Ret': 'retired',
    }

    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None

                    # Get election type
                    row[2] = self.election_types.get(row[2])
                    
                    # Get race type
                    row[4] = self.race_types.get(row[4])
                    
                    # Get outcome
                    if row[5] == 'Won':
                        row[5] = 'won'
                    elif row[5] == 'Lost':
                        row[5] = 'lost'
                    else:
                        row[5] = None

                    yield dict(zip(self.header, row))


class SunshineCandidateCommittees(SunshineTransformLoad):
    table_name = 'candidate_committees'
    header = ['committee_id', 'candidate_id']
    filename = 'CmteCandidateLinks.txt_latest.txt'
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None
                    row.pop(0)
                    yield dict(zip(self.header, row))

    @property
    def upsert(self):
        field_format = '{1} = subq.{1}'
        
        update_fields = [field_format.format(self.table_name,f) \
                             for f in self.header]
        
        where_clause = ''' 
            WHERE {0}.{1} = subq.{1}
              AND {0}.{2} = subq.{2}
        '''.format(self.table_name, 
                   self.header[0], 
                   self.header[1])

        return ''' 
            WITH upsert AS (
              UPDATE {0} SET 
                {1}
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              {2}
              RETURNING *
            )
            INSERT INTO {0} 
              SELECT * FROM temp_{0}
            WHERE NOT EXISTS (SELECT * FROM upsert)
        '''.format(self.table_name, 
                   ','.join(update_fields),
                   where_clause)

class SunshineOfficerCommittees(SunshineTransformLoad):
    table_name = 'officers'
    header = ['committee_id', 'officer_id']
    filename = 'CmteOfficerLinks.txt_latest.txt'
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None
                    row.pop(0)
                    yield dict(zip(self.header, row))

    def createTempTable(self):
        create = ''' 
            CREATE TABLE temp_{0} (
              committee_id INTEGER, 
              officer_id INTEGER
            )
        '''.format(self.table_name)
        with self.engine.begin() as conn:
            conn.execute('DROP TABLE IF EXISTS temp_{0}'.format(self.table_name))
            conn.execute(create)
    
    @property
    def upsert(self):

        return ''' 
              UPDATE officers SET 
                committee_id = subq.committee_id
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              WHERE officers.id = subq.officer_id
        '''.format(self.table_name)

if __name__ == "__main__":
    import sys
    from sunshine import app_config 
    from sunshine.database import engine, Base

    extract = SunshineExtract(ftp_host=app_config.FTP_HOST,
                              ftp_path=app_config.FTP_PATH,
                              ftp_user=app_config.FTP_USER,
                              ftp_pw=app_config.FTP_PW,
                              aws_key=app_config.AWS_KEY,
                              aws_secret=app_config.AWS_SECRET)
    
    committees = SunshineCommittees(engine, 
                                    Base.metadata)
    committees.load()
    committees.update()
    
    candidates = SunshineCandidates(engine, 
                                    Base.metadata)
    candidates.load()
    candidates.update()
    
    officers = SunshineOfficers(engine, 
                                Base.metadata)
    officers.load()
    officers.update()
    
    prev_off = SunshinePrevOfficers(engine, 
                                    Base.metadata)
    prev_off.load()
    prev_off.update()
    
    candidacy = SunshineCandidacy(engine, 
                                  Base.metadata)
    candidacy.load()
    candidacy.update()
    
    can_cmte_xwalk = SunshineCandidateCommittees(engine, 
                                                 Base.metadata)
    can_cmte_xwalk.load()
    can_cmte_xwalk.update()
    
    off_cmte_xwalk = SunshineOfficerCommittees(engine, 
                                               Base.metadata)
    off_cmte_xwalk.load()
    off_cmte_xwalk.update()
