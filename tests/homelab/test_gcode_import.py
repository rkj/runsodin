#!/usr/bin/env python3
"""Offline import and exact-printer regressions against Odin's bootstrap schema.
Run in disposable Odin image with no networking and no persistent data mounted.
"""
import asyncio
import inspect
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

class GcodeImports(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory(prefix='odin-gcode-test-')
  cls.root=Path(sys.argv[1] if len(sys.argv)>1 else '/app')
  backend=cls.root/'backend' if (cls.root/'backend').is_dir() else cls.root
  sys.path.insert(0,str(backend))
  os.environ['DATABASE_URL']='sqlite:///'+cls.tmp.name+'/test.db'
  os.environ['JWT_SECRET_KEY']='offline-synthetic-jwt-secret-for-tests-only'
  from sqlalchemy import text
  from sqlalchemy.orm import Session
  from starlette.requests import Request
  from starlette.datastructures import UploadFile
  from core.db import engine
  from core.schema.bootstrap import bootstrap_database
  from modules.models_library.routes.print_files import upload_3mf
  from modules.models_library.routes.models_crud import schedule_from_model
  from modules.printers import dispatch
  from modules.jobs.scheduler import Scheduler
  cls.text=staticmethod(text); cls.Session=Session; cls.UploadFile=UploadFile
  cls.request=Request({'type':'http','method':'POST','path':'/api/print-files/upload','headers':[],'client':('127.0.0.1',9000)})
  cls.engine=engine; cls.upload_endpoint=staticmethod(inspect.unwrap(upload_3mf));cls.schedule=staticmethod(schedule_from_model); cls.dispatch=dispatch; cls.Scheduler=Scheduler
  bootstrap_database(engine,backend)
  with engine.begin() as c:
   c.execute(text("INSERT INTO groups(id,name) VALUES(7,'test group'),(8,'other group')"))
   c.execute(text("INSERT INTO printers(id,name,api_type,org_id,is_active) VALUES(42,'Target U1','moonraker',7,1),(43,'Other U1','moonraker',7,1),(44,'Prusa','prusalink',7,1),(45,'Private printer','moonraker',8,1)"))
 @classmethod
 def tearDownClass(cls):
  cls.engine.dispose(); cls.tmp.cleanup()
 def setUp(self):
  with self.engine.begin() as c:
   for table in ('print_jobs','jobs','models','print_files'):
    c.execute(self.text('DELETE FROM '+table))
  self.db=self.Session(self.engine)
  self.user={'role':'operator','group_id':7}
  self.paths=[]
 def tearDown(self):
  self.db.rollback(); self.db.close()
  for p in self.paths:
   Path(p).unlink(missing_ok=True)
 def upload(self, target=42,name='test.gcode',data=b'; printer_model = Test U1\nG28\n'):
  result=asyncio.run(type(self).upload_endpoint(request=self.request,file=self.UploadFile(filename=name,file=io.BytesIO(data)),target_printer_id=target,current_user=self.user,db=self.db))
  p=self.db.execute(self.text('SELECT stored_path FROM print_files WHERE id=:id'),{'id':result['id']}).scalar_one();self.paths.append(p)
  return result
 def test_required_printer_and_binary_format(self):
  from fastapi import HTTPException
  with self.assertRaises(HTTPException) as err:self.upload(None)
  self.assertEqual(err.exception.status_code,400)
  with self.assertRaises(HTTPException) as err:self.upload(42,'test.bgcode',b'GCDE')
  self.assertEqual(err.exception.status_code,400)
  binary=self.upload(44,'test.bgcode',b'GCDE')
  self.assertEqual(binary['target_printer_id'],44)
 def test_org_access(self):
  with self.assertRaises(Exception) as err:self.upload(45)
  self.assertEqual(getattr(err.exception,'status',getattr(err.exception,'status_code',None)),404)
  self.assertEqual(self.db.execute(self.text('SELECT count(*) FROM print_files')).scalar_one(),0)
 def test_same_name_retains_separate_files_and_org(self):
  a=self.upload(); b=self.upload(43,data=b'G28\n; distinct second artifact\n')
  self.assertNotEqual(a['model_id'],b['model_id'])
  rows=self.db.execute(self.text('SELECT m.id,m.print_file_id,m.org_id,pf.org_id,pf.compatibility_facts_json FROM models m JOIN print_files pf ON pf.id=m.print_file_id ORDER BY m.id')).all()
  self.assertEqual([r[1] for r in rows],[a['id'],b['id']]);self.assertEqual([r[2:4] for r in rows],[(7,7),(7,7)])
  self.assertEqual([json.loads(r[4])['target_printer_id'] for r in rows],[42,43])
  self.assertEqual(self.db.execute(self.text('SELECT count(*) FROM jobs')).scalar_one(),0)
 def test_schedule_target_and_reject_other(self):
  from fastapi import HTTPException
  a=self.upload()
  with self.assertRaises(HTTPException) as err:self.schedule(a['model_id'],printer_id=43,current_user=self.user,db=self.db)
  self.assertEqual(err.exception.status_code,400)
  result=self.schedule(a['model_id'],printer_id=None,current_user=self.user,db=self.db)
  self.assertEqual(self.db.execute(self.text('SELECT printer_id FROM jobs WHERE id=:id'),{'id':result['job_id']}).scalar_one(),42)
 def test_dispatch_rejects_reassigned_job_before_network(self):
  a=self.upload(); job=self.schedule(a['model_id'],printer_id=None,current_user=self.user,db=self.db)
  self.db.execute(self.text("UPDATE jobs SET printer_id=43,status='scheduled' WHERE id=:id"),{'id':job['job_id']});self.db.commit()
  with mock.patch.object(self.dispatch,'_get_printer_info',return_value={'api_type':'moonraker'}), mock.patch.object(self.dispatch,'_dispatch_moonraker',side_effect=AssertionError('network forbidden')) as network:
   ok,message=self.dispatch.dispatch_job(43,job['job_id'])
  self.assertFalse(ok);self.assertIn('another printer',message);network.assert_not_called()
 def test_dispatch_uses_canonical_file(self):
  a=self.upload(); b=self.upload(43,data=b'G28\n; variant\n')
  job=self.schedule(a['model_id'],printer_id=None,current_user=self.user,db=self.db)
  self.db.execute(self.text('UPDATE print_files SET model_id=:model WHERE id=:id'),{'model':a['model_id'],'id':b['id']})
  self.db.execute(self.text("UPDATE jobs SET status='scheduled' WHERE id=:id"),{'id':job['job_id']});self.db.commit()
  self.assertEqual(self.dispatch._load_job(job['job_id'])['stored_path'],self.paths[0])
  self.assertEqual(self.dispatch._get_next_scheduled_job(42)['stored_path'],self.paths[0])
 def test_selected_bytes_and_uppercase_extension_reach_adapter(self):
  content=b'G28\n; exact synthetic artifact\n'
  a=self.upload(name='UPPER.GCODE',data=content)
  job=self.schedule(a['model_id'],printer_id=None,current_user=self.user,db=self.db)
  self.db.execute(self.text("UPDATE jobs SET status='scheduled' WHERE id=:id"),{'id':job['job_id']});self.db.commit()
  seen=[]
  def adapter(job_id,path,name,creds):
   seen.append((Path(path).read_bytes(),name));return True,'mock accepted'
  with mock.patch.object(self.dispatch,'_get_printer_info',return_value={'api_type':'moonraker','ip':'printer.invalid'}),mock.patch.object(self.dispatch,'_dispatch_moonraker',side_effect=adapter),mock.patch.object(self.dispatch,'_ws'):
   ok,message=self.dispatch.dispatch_job(42,job['job_id'])
  self.assertTrue(ok,message);self.assertEqual(seen,[(content,'UPPER.GCODE')])
 def test_scheduler_preserves_target_and_skips_unavailable(self):
  a=self.upload();job=self.schedule(a['model_id'],printer_id=None,current_user=self.user,db=self.db)
  result=self.Scheduler().run(self.db)
  self.assertEqual(result.scheduled_count,1)
  self.assertEqual(self.db.execute(self.text('SELECT printer_id FROM jobs WHERE id=:id'),{'id':job['job_id']}).scalar_one(),42)
  self.db.execute(self.text("UPDATE jobs SET status='pending',scheduled_start=NULL,scheduled_end=NULL WHERE id=:id"),{'id':job['job_id']})
  self.db.execute(self.text('UPDATE printers SET is_active=0 WHERE id=42'));self.db.commit()
  try:
   result=self.Scheduler().run(self.db)
   self.assertEqual(result.scheduled_count,0)
   self.assertEqual(self.db.execute(self.text('SELECT printer_id FROM jobs WHERE id=:id'),{'id':job['job_id']}).scalar_one(),42)
  finally:
   self.db.execute(self.text('UPDATE printers SET is_active=1 WHERE id=42'));self.db.commit()

if __name__=='__main__': unittest.main(argv=[sys.argv[0]],verbosity=2)
