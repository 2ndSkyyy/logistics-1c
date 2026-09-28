#!/usr/bin/env python3
"""Структурная проверка поставки. Не компилирует и не исполняет код 1С."""
from pathlib import Path
from collections import Counter
from decimal import Decimal
from datetime import datetime
from uuid import UUID
from urllib.parse import unquote
import json
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MD = {'m': 'http://v8.1c.ru/8.3/MDClasses', 'v8': 'http://v8.1c.ru/8.1/data/core'}
errors = []
checks = []


def require(condition, message):
    if not condition:
        errors.append(message)


def xml(path):
    return ET.parse(path).getroot()


def attr_type(path, name):
    root = xml(path)
    for attribute in root.findall('.//m:Attribute', MD):
        props = attribute.find('m:Properties', MD)
        if props.findtext('m:Name', namespaces=MD) == name:
            return props.find('m:Type', MD)
    raise ValueError(f'Нет реквизита {name}: {path}')


def main():
    xml_files = sorted((ROOT / 'src').rglob('*.xml'))
    for path in xml_files:
        try:
            xml(path)
        except ET.ParseError as exc:
            errors.append(f'Некорректный XML {path.relative_to(ROOT)}: {exc}')
    checks.append(f'XML parsed: {len(xml_files)}')

    expected = {'logistics': 'УправлениеЛогистикой', 'bp-extension': 'ЛогистикаОбмен'}
    plurals = {'Language': 'Languages', 'Subsystem': 'Subsystems', 'Catalog': 'Catalogs',
               'Document': 'Documents', 'Report': 'Reports', 'DataProcessor': 'DataProcessors',
               'Enum': 'Enums'}
    configurations = {}
    for directory, name in expected.items():
        base = ROOT / 'src' / directory
        root = xml(base / 'Configuration.xml')
        config = root.find('m:Configuration', MD)
        require(config.findtext('m:Properties/m:Name', namespaces=MD) == name,
                f'Неверная конфигурация в {directory}')
        children = config.find('m:ChildObjects', MD)
        counts = Counter(child.tag.split('}')[-1] for child in children)
        configurations[directory] = dict(counts)
        for child in children:
            category = child.tag.split('}')[-1]
            require(category in plurals, f'Неожиданный вид метаданных: {category}')
            if category in plurals:
                require((base / plurals[category] / (child.text + '.xml')).is_file(),
                        f'Нет файла объекта: {directory}/{category}.{child.text}')
        if directory == 'bp-extension':
            require(config.findtext('m:Properties/m:ConfigurationExtensionPurpose', namespaces=MD)
                    == 'AddOn', 'Назначение расширения не AddOn')
            require(counts['Document'] == 2 and counts['DataProcessor'] == 1,
                    'Неожиданный состав расширения')
        else:
            require(counts['Catalog'] == 7 and counts['Document'] == 1,
                    'Неожиданный состав логистики')
    checks.append('Configuration names, kind and top-level metadata files')

    form_count = 0
    for path in (ROOT / 'src').rglob('Form.xml'):
        module = path.parent / 'Form' / 'Module.bsl'
        text = module.read_text(encoding='utf-8-sig') if module.exists() else ''
        names = re.findall(r'^\s*(?:Процедура|Функция)\s+(\w+)\s*\(', text, re.M)
        require(len(names) == len(set(names)), f'Дубликаты процедур: {module.relative_to(ROOT)}')
        for node in xml(path).iter():
            if node.tag.split('}')[-1] in ('Action', 'Event') and (node.text or '').strip():
                action = node.text.strip()
                require(action in names, f'Нет обработчика {action}: {path.relative_to(ROOT)}')
        form_count += 1
    checks.append(f'Local form action/event bindings: {form_count}')

    employee_type = attr_type(ROOT / 'src/logistics/Catalogs/Сотрудники.xml', 'Телефон')
    require(employee_type.findtext('v8:Type', namespaces=MD) == 'xs:string', 'Телефон должен быть строкой')
    require(employee_type.findtext('v8:StringQualifiers/v8:Length', namespaces=MD) == '50',
            'Длина телефона должна быть 50')
    email_type = attr_type(ROOT / 'src/logistics/Catalogs/Контрагенты.xml', 'ЭлектроннаяПочта')
    require(int(email_type.findtext('v8:StringQualifiers/v8:Length', namespaces=MD)) >= 254,
            'Недостаточная длина электронной почты')
    checks.append('Phone and email metadata corrections')

    forward = json.loads((ROOT / 'examples/Логистика_Заявка_тест.json').read_text('utf-8-sig'), parse_float=Decimal)
    reverse = json.loads((ROOT / 'examples/БП_Результат_тест.json').read_text('utf-8-sig'), parse_float=Decimal)
    require(forward['ВерсияФормата'] == reverse['ВерсияФормата'] == 1, 'Неверная версия примеров')
    require(forward['Направление'] == 'ЛогистикаВБухгалтерию', 'Неверное прямое направление')
    require(reverse['Направление'] == 'БухгалтерияВЛогистику', 'Неверное обратное направление')
    require(reverse['РежимПередачи'] == 'ПолныйСнимок', 'Неверный режим снимка')
    source, response = forward['Заявка'], reverse['Заявка']
    require(UUID(source['Идентификатор']) == UUID(response['ИдентификаторЛогистики']), 'Не совпадают UUID заявок')
    require(UUID(source['Реквизиты']['ИдентификаторЗаявкиБП']) == UUID(response['ИдентификаторБП']), 'Не совпадает UUID БП')
    require(source['Реквизиты']['СуммаЗаявки'] == response['СуммаЗаявки'], 'Не совпадают стоимости')
    payments_sum = sum(p['Сумма'] for p in response['Оплаты'])
    require(payments_sum == response['СуммаОплаты'], 'Не сходится сумма платежей')
    require(response['СуммаЗаявки'] - payments_sum == response['ОстатокКОплате'], 'Не сходится остаток')
    for key in ('Оплаты', 'БухгалтерскиеДокументы'):
        rows = response[key]
        ids = [UUID(row['ИдентификаторБП']) for row in rows]
        require(len(ids) == len(set(ids)), f'Повтор UUID: {key}')
        for row in rows:
            require(type(row['Сумма']) in (int, Decimal) and row['Сумма'] >= 0, f'Неверная сумма: {key}')
            datetime.strptime(row['Дата'], '%Y-%m-%dT%H:%M:%S')
    require(datetime.fromisoformat(reverse['ДатаВыгрузки']) >=
            datetime.fromisoformat(source['Реквизиты']['ДатаОбменаБП']), 'Пример ответа старее предыдущего обмена')
    checks.append('Example UUIDs, dates, directions, unique row IDs and payment arithmetic')

    schema = xml(ROOT / 'src/logistics/Reports/ОплатаЗаявок/Templates/ОсновнаяСхемаКомпоновкиДанных/Ext/Template.xml')
    query = next(n.text for n in schema.iter() if n.tag.split('}')[-1] == 'query')
    require('НАЧАЛОПЕРИОДА(&НачалоПериода, ДЕНЬ)' in query and
            'КОНЕЦПЕРИОДА(&КонецПериода, ДЕНЬ)' in query, 'Не обнаружены обе границы периода')
    checks.append('Report period query boundaries')

    link_count = 0
    for path in ROOT.rglob('*.md'):
        for target in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)\)', path.read_text('utf-8')):
            if '://' in target or target.startswith(('#', 'mailto:')):
                continue
            local = unquote(target.split('#', 1)[0])
            require((path.parent / local).exists(), f'Нерабочая ссылка {target}: {path.relative_to(ROOT)}')
            link_count += 1
    checks.append(f'Local Markdown links: {link_count}')

    prohibited = {'.1cd', '.dt', '.cf', '.cfu', '.cfe', '.zip', '.7z', '.rar'}
    for path in ROOT.rglob('*'):
        if '.git' in path.relative_to(ROOT).parts:
            continue
        if path.is_file():
            require(path.suffix.lower() not in prohibited, f'Лишний файл поставки: {path.relative_to(ROOT)}')
    checks.append('No database backups, compiled builds or nested archives')

    result = {'status': 'passed' if not errors else 'failed',
              'scope': 'Static structure checks only; no 1C runtime or BSL compilation',
              'checks': checks, 'metadata_counts': configurations,
              'example': {'invoices': len(response['БухгалтерскиеДокументы']),
                          'payments': len(response['Оплаты']), 'paid': str(payments_sum),
                          'balance': str(response['ОстатокКОплате'])},
              'errors': errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, StopIteration, ET.ParseError) as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
