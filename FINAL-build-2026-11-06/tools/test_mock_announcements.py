import runpy, sys, unittest
from pathlib import Path
from flask import Flask

class MockAnnouncementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old_args=sys.argv[:]
        old_run=Flask.run
        Flask.run=lambda *args,**kwargs: None
        try:
            sys.argv=['mock_server.py',str(Path('.').resolve()),'5000']
            cls.app=runpy.run_path('tools/mock_server.py')['app']
        finally:
            Flask.run=old_run
            sys.argv=old_args

    def setUp(self):
        self.client=self.app.test_client()
        self.client.post('/api/login',json={'email':'admin@astanait.edu.kz'})
        self.admin={'Authorization':'mock-admin'}
        self.client.post('/api/login',json={'email':'260002@astanait.edu.kz'})
        self.student={'Authorization':'mock-student'}
        self.client.post('/api/login',json={'email':'teacher@astanait.edu.kz'})
        self.teacher={'Authorization':'mock-teacher'}

    def test_all_roles_read_a_feed_and_unread_count_changes(self):
        for headers in (self.admin,self.student,self.teacher):
            r=self.client.get('/api/announcements?offset=0&limit=10',headers=headers)
            self.assertEqual(r.status_code,200);self.assertIsInstance(r.json,list)
        student_feed=self.client.get('/api/announcements',headers=self.student).json
        self.assertEqual(student_feed[0]['title'],'Welcome to the LMS')
        count=self.client.get('/api/announcements/unread-count',headers=self.student).json['count']
        self.assertGreater(count,0)
        self.client.post(f"/api/announcements/{student_feed[0]['id']}/read",headers=self.student)
        self.assertEqual(self.client.get('/api/announcements/unread-count',headers=self.student).json['count'],count-1)

    def test_admin_crud_and_role_denials(self):
        body={'title':'Exam update','body':'Dates are posted at https://example.edu/exams','category':'Exams','audience':'everyone','pinned':True,'expires_at':None}
        created=self.client.post('/api/announcements',json=body,headers=self.admin)
        self.assertEqual(created.status_code,201);ident=created.json['id']
        self.assertIn(ident,[x['id'] for x in self.client.get('/api/announcements',headers=self.student).json])
        updated=dict(body,title='Updated exam update')
        self.assertEqual(self.client.put(f'/api/announcements/{ident}',json=updated,headers=self.admin).status_code,200)
        self.assertEqual(self.client.delete(f'/api/announcements/{ident}',headers=self.admin).status_code,200)
        for headers in (self.student,self.teacher):
            self.assertEqual(self.client.post('/api/announcements',json=body,headers=headers).status_code,403)
            self.assertEqual(self.client.put('/api/announcements/1',json=body,headers=headers).status_code,403)
            self.assertEqual(self.client.delete('/api/announcements/1',headers=headers).status_code,403)

    def test_unauthorized_and_unknown_routes(self):
        self.assertEqual(self.client.get('/api/announcements').status_code,401)
        self.assertEqual(self.client.post('/api/announcements',json={}).status_code,401)
        self.assertEqual(self.client.get('/api/xyz').status_code,404)

if __name__=='__main__': unittest.main()
