import unittest
import zipfile
import io
import os
from bs4 import BeautifulSoup, NavigableString

class TestEpubFixer(unittest.TestCase):
    def test_inplace_html_text_replacement(self):
        html_doc = '<html><head><link rel="stylesheet" href="style.css"/></head><body><h1>Chapter 1</h1><p class="dialogue">“俊希，今天天气不错。”</p><p>这是俊希的房间。</p></body></html>'
        soup = BeautifulSoup(html_doc, 'html.parser')
        
        replacement_map = {'俊希': '俊熙'}
        
        for text_node in soup.find_all(string=True):
            if isinstance(text_node, NavigableString) and text_node.parent.name not in ['script', 'style']:
                original = str(text_node)
                modified = original
                for k, v in replacement_map.items():
                    if k in modified:
                        modified = modified.replace(k, v)
                if modified != original:
                    text_node.replace_with(modified)
        
        result_html = str(soup)
        self.assertIn('“俊熙，今天天气不错。”', result_html)
        self.assertIn('这是俊熙的房间。', result_html)
        self.assertNotIn('俊希', result_html)
        self.assertIn('class="dialogue"', result_html)
        self.assertIn('style.css', result_html)

    def test_zip_container_processing(self):
        zip_buffer = io.BytesIO()
        fake_html = '<html><head><link rel="stylesheet" href="style.css"/></head><body><h1>Title</h1><p class="intro">俊希说道：你好。</p></body></html>'.encode('utf-8')
        fake_css = b'body { font-size: 1em; }'
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('mimetype', b'application/epub+zip')
            zf.writestr('OEBPS/style.css', fake_css)
            zf.writestr('OEBPS/chapter01.xhtml', fake_html)
            
        zip_buffer.seek(0)
        
        out_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'r') as zin, zipfile.ZipFile(out_buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.endswith(('.xhtml', '.html')):
                    soup = BeautifulSoup(data.decode('utf-8'), 'html.parser')
                    for node in soup.find_all(string=True):
                        if isinstance(node, NavigableString) and node.parent.name not in ['script', 'style']:
                            if '俊希' in str(node):
                                node.replace_with(str(node).replace('俊希', '俊熙'))
                    data = str(soup).encode('utf-8')
                zout.writestr(item, data)
                
        out_buffer.seek(0)
        with zipfile.ZipFile(out_buffer, 'r') as zcheck:
            chap_content = zcheck.read('OEBPS/chapter01.xhtml').decode('utf-8')
            self.assertIn('俊熙说道：你好。', chap_content)
            self.assertNotIn('俊希', chap_content)
            self.assertIn('class="intro"', chap_content)
            self.assertEqual(zcheck.read('OEBPS/style.css'), fake_css)

if __name__ == '__main__':
    unittest.main()
