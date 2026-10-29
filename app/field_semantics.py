"""Source-preserving syntax and narrow, explicit clause semantics; no answer access."""
import re
import unicodedata

IDENTIFIER_POLICY = 'identifier-year-brackets-v1'


def canonical_identifier(value):
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value) if value is not None else ''))
    # Only a balanced bracket pair around a four-digit year. Never repair any digit.
    for left, right in [('(', ')'), ('〔', '〕'), ('[', ']'), ('【', '】')]:
        text = re.sub(re.escape(left) + r'(\d{4})' + re.escape(right), r'〔\1〕', text)
    return text


def explicit_modes(raw):
    text = unicodedata.normalize('NFKC', str(raw))
    text = re.sub(r'[ \t]+', '', text)
    found = []
    for sentence in re.split(r'[。；;\n]', text):
        if re.search(r'并非|不是|不适用|是否|尚未确定', sentence):
            continue
        if re.search(r'(?:合同期内)?年租金(?:标准)?(?:不作调整|不做调整|保持不变|固定不变|不予调整)', sentence):
            found.append('固定')
        if ('不随' not in sentence and re.search(r'(?:年租金|年租金标准|租金标准).{0,25}随.{0,25}(?:基准地价|计租标准|基准租金|标准).{0,15}调整', sentence)):
            found.append('随标准调整')
        if re.search(r'[☑✓√]固定', sentence):
            found.append('固定')
        if re.search(r'[☑✓√]随(?:基准地价|标准)(?:调整)?', sentence):
            found.append('随标准调整')
    return list(dict.fromkeys(found))
