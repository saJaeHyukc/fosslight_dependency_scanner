#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2021 LG Electronics Inc.
# SPDX-License-Identifier: Apache-2.0

import os
import logging
import json
import re
import shutil
import yaml
import subprocess
import fosslight_util.constant as constant
import fosslight_dependency.constant as const
from fosslight_dependency._package_manager import PackageManager
from fosslight_dependency._package_manager import check_and_run_license_scanner, get_url_to_purl

logger = logging.getLogger(constant.LOGGER_NAME)


class Pub(PackageManager):
    package_manager_name = const.PUB

    dn_url = 'https://pub.dev/packages/'
    input_file_name = 'tmp_flutter_oss_licenses.json'
    tmp_dir = "fl_dependency_tmp_dir"
    cur_path = ''

    def __init__(self, input_dir, output_dir):
        super().__init__(self.package_manager_name, self.dn_url, input_dir, output_dir)
        self.append_input_package_list_file(self.input_file_name)

    def __del__(self):
        if self.cur_path != '':
            os.chdir(self.cur_path)
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)

    def run_plugin(self):
        if os.path.exists(self.input_file_name):
            logger.info(f"Found {self.input_file_name}, skip the flutter cmd to analyze dependency.")
            return True

        if not os.path.exists(const.SUPPORT_PACKAE.get(self.package_manager_name)):
            logger.error(f"Cannot find the file({const.SUPPORT_PACKAE.get(self.package_manager_name)})")
            return False

        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)

        os.mkdir(self.tmp_dir)
        shutil.copy(const.SUPPORT_PACKAE.get(self.package_manager_name),
                    os.path.join(self.tmp_dir, const.SUPPORT_PACKAE.get(self.package_manager_name)))

        self.cur_path = os.getcwd()
        os.chdir(self.tmp_dir)

        with open(const.SUPPORT_PACKAE.get(self.package_manager_name), 'r', encoding='utf8') as f:
            tmp_yml = yaml.safe_load(f)
            tmp_yml['dev_dependencies'] = {'flutter_oss_licenses': '^2.0.1'}
        with open(const.SUPPORT_PACKAE.get(self.package_manager_name), 'w', encoding='utf8') as f:
            f.write(yaml.dump(tmp_yml))

        cmd = "flutter pub get"
        ret = subprocess.call(cmd, shell=True)
        if ret != 0:
            logger.error(f"Failed to run: {cmd}")
            os.chdir(self.cur_path)
            return False

        cmd = f"flutter pub run flutter_oss_licenses:generate.dart -o {self.input_file_name} --json"
        ret = subprocess.call(cmd, shell=True)
        if ret != 0:
            logger.error(f"Failed to run: {cmd}")
            os.chdir(self.cur_path)
            return False

        return True

    def parse_pub_deps_file(self, rel_json):
        name_version_dict = {}
        try:
            for p in rel_json['packages']:
                if p['kind'] == 'root':
                    self.package_name = p['name']
                name_version_dict[p['name']] = p['version']
                if p['dependencies'] == []:
                    continue
                dep_key = f"{p['name']}({p['version']})"
                if dep_key not in self.relation_tree:
                    self.relation_tree[dep_key] = []
                self.relation_tree[dep_key].extend(p['dependencies'])

            for i in self.relation_tree:
                tmp_dep = []
                for d in self.relation_tree[i]:
                    d_ver = name_version_dict[d]
                    tmp_dep.append(f'{d}({d_ver})')
                self.relation_tree[i] = []
                self.relation_tree[i].extend(tmp_dep)
        except Exception as e:
            logger.error(f'Failed to parse dependency tree: {e}')

    def parse_oss_information(self, f_name):
        tmp_license_txt_file_name = 'tmp_license.txt'
        json_data = ''
        comment = ''

        with open(f_name, 'r', encoding='utf8') as pub_file:
            json_f = json.load(pub_file)

        try:
            sheet_list = []

            for json_data in json_f:
                oss_origin_name = json_data['name']
                if oss_origin_name not in self.total_dep_list:
                    continue
                oss_name = f"{self.package_manager_name}:{oss_origin_name}"
                oss_version = json_data['version']
                homepage = json_data['homepage']
                if homepage is None:
                    homepage = json_data['repository']
                if homepage is None:
                    homepage = ''
                dn_loc = f"{self.dn_url}{oss_origin_name}/versions/{oss_version}"
                purl = get_url_to_purl(dn_loc, self.package_manager_name)
                self.purl_dict[f'{oss_origin_name}({oss_version})'] = purl
                license_txt = json_data['license']

                tmp_license_txt = open(tmp_license_txt_file_name, 'w', encoding='utf-8')
                tmp_license_txt.write(license_txt)
                tmp_license_txt.close()

                license_name_with_license_scanner = check_and_run_license_scanner(self.platform,
                                                                                  self.license_scanner_bin,
                                                                                  tmp_license_txt_file_name)

                if license_name_with_license_scanner != "":
                    license_name = license_name_with_license_scanner
                else:
                    license_name = ''

                comment_list = []
                deps_list = []
                if self.direct_dep:
                    if oss_origin_name not in self.total_dep_list:
                        continue
                    if self.package_name == f'{oss_origin_name}({oss_version})':
                        comment_list.append('root package')
                    else:
                        if json_data['isDirectDependency']:
                            comment_list.append('direct')
                        else:
                            comment_list.append('transitive')

                    if f'{oss_origin_name}({oss_version})' in self.relation_tree:
                        deps_list.extend(self.relation_tree[f'{oss_origin_name}({oss_version})'])
                comment = ','.join(comment_list)
                sheet_list.append([purl, oss_name, oss_version, license_name, dn_loc, homepage,
                                  '', '', comment, deps_list])
        except Exception as e:
            logger.error(f"Fail to parse pub oss information: {e}")
        sheet_list = self.change_dep_to_purl(sheet_list)

        if os.path.isfile(tmp_license_txt_file_name):
            os.remove(tmp_license_txt_file_name)

        return sheet_list

    def parse_no_dev_command_file(self, pub_deps):
        for line in pub_deps.split('\n'):
            re_result = re.findall(r'\-\s(\S+)\s', line)
            if re_result:
                self.total_dep_list.append(re_result[0])
        self.total_dep_list = list(set(self.total_dep_list))

    def parse_direct_dependencies(self):
        self.direct_dep = True
        tmp_pub_deps_file = 'tmp_deps.json'
        tmp_no_dev_deps_file = 'tmp_no_dev_deps.txt'
        encoding_list = ['utf8', 'utf16']
        if os.path.exists(tmp_pub_deps_file) and os.path.exists(tmp_no_dev_deps_file):
            for encode in encoding_list:
                try:
                    logger.info(f'Try to encode with {encode}.')
                    with open(tmp_pub_deps_file, 'r+', encoding=encode) as deps_f:
                        lines = deps_f.readlines()
                        deps_f.seek(0)
                        deps_f.truncate()
                        for num, line in enumerate(lines):
                            if line.startswith('{'):
                                first_line = num
                                break
                        deps_f.writelines(lines[first_line:])
                        deps_f.seek(0)
                        deps_l = json.load(deps_f)
                        self.parse_pub_deps_file(deps_l)
                    with open(tmp_no_dev_deps_file, 'r', encoding=encode) as no_dev_f:
                        self.parse_no_dev_command_file(no_dev_f.read())
                    logger.info('Parse tmp pub deps file.')
                except UnicodeDecodeError as e1:
                    logger.info(f'Fail to encode with {encode}: {e1}')
                    pass
                except Exception as e:
                    logger.error(f'Fail to parse tmp pub deps result file: {e}')
                    return False
                else:
                    logger.info(f'Success to encode with {encode}.')
                    break
        else:
            try:
                cmd = "flutter pub get"
                ret = subprocess.call(cmd, shell=True)
                if ret != 0:
                    logger.error(f"Failed to run: {cmd}")
                    os.chdir(self.cur_path)
                    return False

                cmd = "flutter pub deps --json"
                ret_txt = subprocess.check_output(cmd, text=True, shell=True)
                if ret_txt is not None:
                    deps_l = json.loads(ret_txt)
                    self.parse_pub_deps_file(deps_l)
                else:
                    return False

                cmd = "flutter pub deps --no-dev -s compact"
                ret_no_dev = subprocess.check_output(cmd, text=True, shell=True, encoding='utf8')
                if ret_no_dev != 0:
                    self.parse_no_dev_command_file(ret_no_dev)

            except Exception as e:
                logger.error(f'Fail to run flutter command:{e}')
        return True
