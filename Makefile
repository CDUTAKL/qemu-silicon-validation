# QEMU 虚拟板卡硅后验证 · 便捷入口
# 用法: make build / test / faults / coverage / rtm / report / all / clean

PY := .venv/bin/python
REPORT := report

.PHONY: build test faults coverage rtm report all clean

build:            ## 固件构建 → firmware/build/firmware.elf
	bash firmware/build.sh

test:             ## pytest 全量 → report/junit.xml + pytest_report.html
	mkdir -p $(REPORT)
	$(PY) -m pytest tests/ --junitxml=$(REPORT)/junit.xml \
	  --html=$(REPORT)/pytest_report.html --self-contained-html -q --tb=short

faults:           ## 三档故障注入战役 → report/fault_injection_stats.json
	$(PY) faults/injection_campaign.py

coverage:         ## gcov 覆盖率（POSIX port 双轨）→ report/coverage_summary.json
	bash coverage/run_coverage.sh

rtm:              ## RTM 合并 → report/rtm.md + rtm.json
	$(PY) rtm/rtm_report.py --junit $(REPORT)/junit.xml \
	  --coverage $(REPORT)/coverage_summary.json

report:           ## 总报告 → report/final_report.html
	$(PY) $(REPORT)/final_report.py

all:              ## 一键全流程（等价 python main.py）
	$(PY) main.py

clean:            ## 清理构建与报告产物
	rm -rf firmware/build $(REPORT)/junit.xml $(REPORT)/pytest_report.html \
	  $(REPORT)/final_report.html $(REPORT)/rtm.md $(REPORT)/rtm.json \
	  $(REPORT)/fault_injection_stats.json $(REPORT)/alert_stats.json \
	  $(REPORT)/coverage_summary.json
