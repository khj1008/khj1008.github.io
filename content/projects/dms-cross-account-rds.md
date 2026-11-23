---
title: AWS DMS를 활용한 계정 간 RDS 실시간 마이그레이션 및 동기화
summary: RDS 스냅샷과 AWS DMS CDC를 조합해 AWS 계정 간 MariaDB를 실시간 동기화하며 마이그레이션. binlog retention 초과로 발생한 이벤트 누락 원인과 대응을 정리
field: 문제 해결
competency: cloud
stack: AWS DMS, RDS, MariaDB
order: 7
original: https://medium.com/midastech/dms%EB%A5%BC-%ED%99%9C%EC%9A%A9%ED%95%9C-aws-%EA%B3%84%EC%A0%95-%EA%B0%84-rds-%EB%A7%88%EC%9D%B4%EA%B7%B8%EB%A0%88%EC%9D%B4%EC%85%98-%EB%B0%8F-%EB%8F%99%EA%B8%B0%ED%99%94-b15200cc054d
---
## AWS DMS를 활용한 계정 간 RDS 마이그레이션 실전 가이드

최근 AWS 계정 간 RDS MariaDB 마이그레이션 작업을 진행했습니다. 스냅샷 복제와 DMS CDC를 조합한 방식으로 진행했는데, 예상보다 고려해야 할 부분이 많았습니다. 이 글에서는 실제 작업 과정과 함께 간과하기 쉬운 주의사항들을 공유하려고 합니다.

![](https://cdn-images-1.medium.com/max/1024/1*5XHKkJhEW0S6w94TfrhK7Q.png)

source DB와 target DB, 그리고 마이그레이션을 위한 dms의 provisioned 위치를 나타내는 간략한 아키텍처입니다.

## 마이그레이션 전략 선택하기

AWS DMS를 사용한 마이그레이션에는 크게 두 가지 방식이 있습니다.

<b>Full Load + CDC 방식</b>은 DMS가 처음부터 끝까지 모든 데이터를 복제하고, 복제 중 발생하는 변경사항도 함께 처리합니다. 가장 안전하고 간단하지만, 데이터 크기가 크면 시간이 오래 소요됩니다.

<b>스냅샷 + CDC Only 방식</b>은 먼저 RDS 스냅샷으로 데이터를 빠르게 복사하고, DMS는 그 이후의 변경사항만 처리합니다. 시간과 네트워크 대역폭을 절약할 수 있지만, binlog position 관리가 필요합니다.

저는 데이터 크기가 크지 않았지만 Full Load 시간을 단축하기 위해 스냅샷 방식을 선택했습니다. 이 방식을 선택한다면 binlog position 타이밍이 매우 중요합니다. 이 부분은 뒤에서 자세히 다루겠습니다.

## 소스 DB 준비하기

마이그레이션을 시작하기 전에 소스 RDS를 먼저 준비해야 합니다.

### DMS 전용 사용자 생성

DMS가 소스 DB에 접근할 수 있도록 전용 사용자를 생성합니다. 보안을 위해 필요한 최소 권한만 부여하는 것이 좋습니다.

```sql
CREATE USER 'dms_user'@'%' IDENTIFIED BY 'your_secure_password';
```

```sql
GRANT SELECT, RELOAD, LOCK TABLES, REPLICATION SLAVE, 
      BINLOG MONITOR, SHOW VIEW ON *.* TO 'dms_user'@'%';
```

MariaDB 10.5.2 이상을 사용한다면 BINLOG MONITOR 권한이 필요합니다. 이전 버전에서는 REPLICATION CLIENT를 사용하면 됩니다.

### Binlog 설정

DMS가 변경사항을 추적하려면 binlog가 활성화되어 있어야 합니다. RDS 파라미터 그룹에서 다음 값들을 설정합니다:

- binlog\_format = ROW - DMS는 Row-based 복제만 지원합니다
- binlog\_row\_image = FULL - 전체 행 정보를 기록해야 합니다
- binlog\_checksum = NONE - DMS 호환성을 위해 체크섬을 비활성화합니다

또한 RDS 콘솔에서 자동 백업을 활성화해야 binlog가 생성됩니다. 백업 보존 기간은 1일 이상으로 설정하면 됩니다.

### Binlog Retention 설정

이 부분이 특히 중요합니다. Binlog retention 기간은 스냅샷 생성부터 DMS Task 시작까지의 시간을 충분히 포괄해야 합니다.

```sql
-- 24시간 보존 설정
CALL mysql.rds_set_configuration('binlog retention hours', 24);
```

```sql
-- 현재 설정 확인
CALL mysql.rds_show_configuration;
```

저는 처음에 24시간으로 설정했는데, 스냅샷 생성과 복원에 예상보다 시간이 소요되어 binlog 파일이 삭제되는 상황이 발생했습니다. 여유 있게 48시간 이상으로 설정하는 것을 권장합니다.

## 스냅샷 생성과 Binlog Position 기록

이 단계가 이 방식의 핵심이자 가장 주의해야 할 부분입니다.

### Binlog Position 기록

<b>이 단계에서 실수하면 데이터 정합성 문제가 발생할 수 있습니다.</b>

스냅샷 생성 전에 binlog position을 확인해야 합니다. 스냅샷 생성 후나 생성 중에 확인하면 안 됩니다.

```sql
SHOW MASTER STATUS;
```

출력 결과는 다음과 같은 형태입니다:

```txt
+---------------------------+----------+--------------+------------------+
| File                      | Position | Binlog_Do_DB | Binlog_Ignore_DB |
+---------------------------+----------+--------------+------------------+
| mysql-bin-changelog.171045|      393 |              |                  |
+---------------------------+----------+--------------+------------------+
```

File과 Position 값을 정확히 기록해두세요. 이후 DMS Task를 생성할 때 사용합니다.

> <b>왜 스냅샷 생성 전에 position을 기록해야 할까요?</b>

DMS는 지정한 binlog position <b>이후</b>의 변경사항만 복제합니다. 따라서:

<b>스냅샷 생성 전에 position 기록 (올바른 방법):</b>

- Position 기록: 10:00 (File: 171045, Position: 393)
- 스냅샷 생성 중 변경사항 발생: 10:00\~11:00
- 스냅샷 완료: 11:00 (10:00\~11:00 사이 변경사항 포함)
- CDC 시작: 10:00 이후부터 복제
- 결과: 10:00\~11:00 사이 변경사항이 <b>중복 복제</b>되지만, DMS가 멱등성으로 처리하므로 안전 ✓

<b>스냅샷 완료 후에 position 기록 (잘못된 방법):</b>

- 스냅샷 생성 중 변경사항 발생: 10:00\~11:00
- 스냅샷 완료: 11:00
- Position 기록: 11:05 (File: 171564, Position: 2316)
- CDC 시작: 11:05 이후부터 복제
- 결과: 11:00\~11:05 사이 변경사항 <b>누락</b> ❌

<b>실제 발생한 문제:</b>

- 스냅샷 생성 전 position 기록: 171045:393
- 스냅샷 생성 중 DELETE 발생
- DMS Task 시작 시점에 171045 파일이 이미 삭제됨 (binlog retention 초과)
- DMS가 가장 오래된 파일 171564:2316부터 시작
- 171045\~171564 사이의 DELETE 이벤트 손실
- 결과: 타겟에만 데이터가 남음

<b>핵심:</b> 스냅샷 생성 <b>전</b>에 position을 기록하되, <b>binlog retention을 충분히 길게</b> 설정해야 합니다. 그래야 DMS Task 시작 시점에도 해당 binlog 파일이 보존됩니다.

### 스냅샷 생성

먼저 RDS 스냅샷을 생성합니다. 콘솔에서 간단히 생성할 수 있지만, CLI를 사용하면 자동화에 유리합니다.

```bash
aws rds create-db-snapshot \
  --db-instance-identifier source-db \
  --db-snapshot-identifier migration-snapshot-20260223 \
  --region ap-northeast-2
```

스냅샷 생성은 DB 크기에 따라 수십 분에서 수 시간까지 소요될 수 있습니다.

### 스냅샷 공유 — KMS 암호화 처리

이 단계는 많은 분들이 간과하는 부분입니다. RDS 스냅샷의 KMS 암호화 방식에 따라 절차가 완전히 달라집니다.

### 현재 암호화 방식 확인

먼저 스냅샷이 어떤 KMS 키로 암호화되어 있는지 확인합니다:

```bash
aws rds describe-db-snapshots \
  --db-snapshot-identifier migration-snapshot-20260223 \
  --query 'DBSnapshots[0].{KmsKeyId:KmsKeyId,Encrypted:Encrypted}'
```

출력 예시:

```json
{
  "KmsKeyId": "arn:aws:kms:ap-northeast-2:987654321098:key/aws/rds",
  "Encrypted": true
}
```

key/aws/rds가 포함되어 있으면 AWS 관리형 기본 키(default KMS)입니다.

### Case 1: 커스텀 KMS로 암호화된 경우

커스텀 KMS 키로 암호화되어 있다면 즉시 공유할 수 있습니다.

```bash
# A 계정에서 B 계정으로 공유
aws rds modify-db-snapshot-attribute \
  --db-snapshot-identifier migration-snapshot-20260223 \
  --attribute-name restore \
  --values-to-add 123456789012
```

```bash
# B 계정에서 복사
aws rds copy-db-snapshot \
  --source-db-snapshot-identifier arn:aws:rds:ap-northeast-2:987654321098:snapshot:migration-snapshot-20260223 \
  --target-db-snapshot-identifier migration-snapshot-copy
```

### Case 2: Default KMS로 암호화된 경우 (대부분)

대부분의 RDS는 기본적으로 AWS 관리형 KMS 키로 암호화되어 있습니다. 이 경우 <b>직접 공유가 불가능</b>하므로 다음 단계를 거쳐야 합니다.

<b>1단계: A 계정에서 공유 가능한 KMS 키 생성</b>

```bash
# KMS 키 생성
aws kms create-key \
  --description "RDS snapshot sharing key" \
  --region ap-northeast-2
```

```txt
# 출력에서 KeyId 확인 (예: 12345678-1234-1234-1234-123456789012)
```

```bash
# B 계정에 KMS 키 사용 권한 부여
aws kms create-grant \
  --key-id 12345678-1234-1234-1234-123456789012 \
  --grantee-principal arn:aws:iam::123456789012:root \
  --operations Decrypt CreateGrant
```

<b>2단계: A 계정에서 공유 가능한 KMS로 스냅샷 복사</b>

```bash
aws rds copy-db-snapshot \
  --source-db-snapshot-identifier migration-snapshot-20260223 \
  --target-db-snapshot-identifier migration-snapshot-shared-kms \
  --kms-key-id arn:aws:kms:ap-northeast-2:987654321098:key/12345678-1234-1234-1234-123456789012
```

이 작업은 데이터 크기에 따라 수십 분에서 수 시간이 소요됩니다. 완료될 때까지 대기해야 합니다.

<b>3단계: 공유 가능한 KMS로 암호화된 스냅샷을 B 계정에 공유</b>

```bash
aws rds modify-db-snapshot-attribute \
  --db-snapshot-identifier migration-snapshot-shared-kms \
  --attribute-name restore \
  --values-to-add 123456789012
```

<b>4단계: B 계정에서 자신의 KMS로 스냅샷 복사</b>

```bash
# B 계정에서 실행
aws rds copy-db-snapshot \
  --source-db-snapshot-identifier arn:aws:rds:ap-northeast-2:987654321098:snapshot:migration-snapshot-shared-kms \
  --target-db-snapshot-identifier migration-snapshot-copy \
  --kms-key-id alias/aws/rds
```

alias/aws/rds는 AWS 관리형 기본 키를 의미합니다. 커스텀 KMS를 사용하려면 해당 키의 ARN을 지정하면 됩니다.

<b>주의사항:</b>

- Default KMS 스냅샷은 직접 공유 불가
- 각 복사 단계마다 완료 확인 필수
- KMS 권한 설정 오류 시 B 계정에서 복사 실패

## 타겟 DB 복원 및 권한 설정

B 계정에서 스냅샷으로 RDS를 복원합니다.

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier target-db \
  --db-snapshot-identifier migration-snapshot-copy \
  --db-instance-class db.r5.large \
  --vpc-security-group-ids sg-xxxxx \
  --db-subnet-group-name target-subnet-group \
  --region ap-northeast-2
```

복원이 완료되면 파라미터 그룹과 보안 그룹을 확인하고, 필요하면 수정합니다.

### 타겟 DB 권한 설정

스냅샷으로 복원한 DB에는 소스 DB의 사용자 정보가 그대로 복사됩니다. 하지만 DMS가 타겟 DB에 데이터를 기록하려면 적절한 권한이 필요합니다.

DMS Task에서 사용할 타겟 DB 사용자에게 다음 권한을 부여해야 합니다:

```sql
-- DMS 전용 사용자 생성 (또는 기존 admin 사용자 사용)
CREATE USER 'dms_target_user'@'%' IDENTIFIED BY 'secure_password';
```

```sql
-- 필요한 권한 부여
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER, 
      CREATE TEMPORARY TABLES, LOCK TABLES, EXECUTE, CREATE VIEW, 
      SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, TRIGGER, REFERENCES 
      ON *.* TO 'dms_target_user'@'%';
```

```sql
FLUSH PRIVILEGES;
```

만약 기존 admin 사용자를 사용한다면 이미 충분한 권한이 있을 것입니다. 하지만 보안을 위해 DMS 전용 사용자를 생성하는 것을 권장합니다.

<b>주의:</b> 타겟 DB에 쓰기 권한이 없으면 DMS Task 실행 도중 에러가 발생합니다. Task 시작 전에 권한을 반드시 확인하세요.

## 네트워크 연결 구성

A 계정과 B 계정의 VPC를 연결해야 DMS가 양쪽 DB에 접근할 수 있습니다.

### VPC Peering 설정

VPC Peering은 두 VPC를 직접 연결하는 가장 간단한 방법입니다.

```bash
# A 계정에서 Peering 요청
aws ec2 create-vpc-peering-connection \
  --vpc-id vpc-aaaaa \
  --peer-vpc-id vpc-bbbbb \
  --peer-owner-id 123456789012 \
  --peer-region ap-northeast-2
```

```bash
# B 계정에서 수락
aws ec2 accept-vpc-peering-connection \
  --vpc-peering-connection-id pcx-xxxxx
```

Peering 연결을 생성한 후에는 각 VPC의 라우팅 테이블에 상대방 CIDR 대역으로 향하는 경로를 추가해야 합니다. 이 부분을 누락하면 연결되지 않으므로 주의하세요.

### 보안 그룹 설정

소스 RDS와 타겟 RDS의 보안 그룹에서 DMS Replication Instance의 보안 그룹으로부터 3306 포트 접근을 허용해야 합니다.

```txt
Type: MySQL/Aurora
Port: 3306
Source: DMS Replication Instance 보안 그룹 (sg-xxxxx)
```

## DMS 설정하기

이제 본격적으로 DMS를 설정할 차례입니다.

### Replication Instance 생성

<b>중요한 점:</b> DMS Serverless는 custom CDC start position을 지원하지 않습니다. 스냅샷 방식을 사용하려면 반드시 Provisioned Instance를 사용해야 합니다.

```bash
aws dms create-replication-instance \
  --replication-instance-identifier migration-replication-instance \
  --replication-instance-class dms.t3.medium \
  --allocated-storage 50 \
  --vpc-security-group-ids sg-xxxxx \
  --replication-subnet-group-identifier dms-subnet-group
```

인스턴스 크기는 DB 크기와 변경 속도에 따라 선택합니다. 소규모 DB라면 dms.t3.medium으로 충분하고, 대용량이거나 변경이 빈번하다면 dms.c5.xlarge 이상을 고려하세요.

### Endpoint 생성

Source와 Target Endpoint를 각각 생성해야 합니다.

<b>Source Endpoint:</b>

```bash
aws dms create-endpoint \
  --endpoint-identifier source-mariadb \
  --endpoint-type source \
  --engine-name mariadb \
  --server-name source-db.xxxxx.ap-northeast-2.rds.amazonaws.com \
  --port 3306 \
  --username dms_user \
  --password your_secure_password
```

<b>Target Endpoint:</b>

```bash
aws dms create-endpoint \
  --endpoint-identifier target-mariadb \
  --endpoint-type target \
  --engine-name mariadb \
  --server-name target-db.xxxxx.ap-northeast-2.rds.amazonaws.com \
  --port 3306 \
  --username dms_target_user \
  --password target_password
```

Endpoint를 생성한 후에는 반드시 연결 테스트를 수행하세요.

```bash
aws dms test-connection \
  --replication-instance-arn arn:aws:dms:... \
  --endpoint-arn arn:aws:dms:...
```

연결 테스트가 실패하면 보안 그룹이나 네트워크 설정을 다시 확인해야 합니다.

### Replication Task 생성

Task를 생성할 때 가장 중요한 설정은 CDC start position입니다. 앞서 기록해둔 binlog position을 여기에 입력합니다.

먼저 Table Mappings JSON 파일을 작성합니다. 이 파일은 어떤 테이블을 복제할지 정의합니다.

<b>table-mappings.json:</b>

```json
{
  "rules": [
    {
      "rule-type": "selection",
      "rule-id": "1",
      "rule-name": "include-all-tables",
      "object-locator": {
        "schema-name": "%",
        "table-name": "%"
      },
      "rule-action": "include"
    }
  ]
}
```

다음으로 Task Settings JSON 파일을 작성합니다. 여기서 중요한 설정은 TargetTablePrepMode를 DO\_NOTHING으로 지정하는 것입니다. 스냅샷으로 테이블이 이미 존재하기 때문입니다.

<b>task-settings.json:</b>

```json
{
  "TargetMetadata": {
    "TargetSchema": "",
    "SupportLobs": false,
    "FullLobMode": false,
    "LobChunkSize": 0,
    "LimitedSizeLobMode": false,
    "LobMaxSize": 0,
    "BatchApplyEnabled": false
  },
  "FullLoadSettings": {
    "TargetTablePrepMode": "DO_NOTHING"
  },
  "Logging": {
    "EnableLogging": true,
    "LogComponents": [
      {
        "Id": "SOURCE_CAPTURE",
        "Severity": "LOGGER_SEVERITY_INFO"
      },
      {
        "Id": "TARGET_APPLY",
        "Severity": "LOGGER_SEVERITY_INFO"
      }
    ]
  },
  "ErrorBehavior": {
    "DataErrorPolicy": "LOG_ERROR",
    "RecoverableErrorCount": -1,
    "RecoverableErrorInterval": 5,
    "RecoverableErrorThrottling": true,
    "RecoverableErrorThrottlingMax": 1800,
    "FullLoadIgnoreConflicts": true
  }
}
```

이제 Task를 생성합니다:

```bash
aws dms create-replication-task \
  --replication-task-identifier migration-cdc-task \
  --source-endpoint-arn arn:aws:dms:...:endpoint:source-mariadb \
  --target-endpoint-arn arn:aws:dms:...:endpoint:target-mariadb \
  --replication-instance-arn arn:aws:dms:...:rep:migration-replication-instance \
  --migration-type cdc \
  --table-mappings file://table-mappings.json \
  --replication-task-settings file://task-settings.json \
  --cdc-start-position "mysql-bin-changelog.171045:393"
```

--cdc-start-position에 앞서 기록한 binlog position을 정확히 입력하는 것이 핵심입니다.

## Task 실행 및 모니터링

Task를 시작한 후에는 모니터링이 중요합니다.

```bash
aws dms start-replication-task \
  --replication-task-arn arn:aws:dms:...:task:migration-cdc-task \
  --start-replication-task-type start-replication
```

### CloudWatch로 모니터링하기

DMS는 여러 유용한 메트릭을 CloudWatch로 전송합니다.

- CDCLatencySource: 소스에서 변경사항을 읽는 지연 시간
- CDCLatencyTarget: 타겟에 변경사항을 적용하는 지연 시간
- CDCIncomingChanges: 초당 수신하는 변경사항 수

CDC Latency가 계속 증가한다면 Replication Instance 크기를 늘려야 할 수 있습니다.

CloudWatch Logs에서 실시간 로그를 확인할 수도 있습니다:

```bash
aws logs tail /aws/dms/tasks/migration-cdc-task --follow
```

### Table Statistics 확인

각 테이블별로 얼마나 복제되었는지 확인할 수 있습니다:

```bash
aws dms describe-table-statistics \
  --replication-task-arn arn:aws:dms:...:task:migration-cdc-task
```

Insert, Update, Delete 건수를 보면 복제가 정상적으로 진행되고 있는지 확인할 수 있습니다.

## 데이터 정합성 검증

CDC가 안정적으로 동작하고 있다면, 이제 데이터가 정확히 복제되었는지 확인해야 합니다.

### 행 수 비교

가장 간단한 방법은 테이블별 행 수를 비교하는 것입니다.

```sql
-- 소스 DB와 타겟 DB에서 각각 실행
SELECT 
  TABLE_SCHEMA,
  TABLE_NAME,
  TABLE_ROWS
FROM information_schema.TABLES
WHERE TABLE_SCHEMA NOT IN ('mysql', 'information_schema', 'performance_schema')
ORDER BY TABLE_SCHEMA, TABLE_NAME;
```

결과를 비교해서 차이가 있는지 확인합니다.

### 체크섬 비교

더 정확한 검증을 위해 체크섬을 사용할 수 있습니다:

```sql
CHECKSUM TABLE database.important_table1, database.important_table2;
```

소스와 타겟에서 같은 쿼리를 실행해서 체크섬이 일치하는지 확인합니다.

### 샘플 데이터 확인

최근 변경된 데이터를 직접 확인하는 것도 효과적입니다:

```sql
SELECT * FROM important_table 
WHERE updated_at > '2026-02-23' 
ORDER BY updated_at DESC 
LIMIT 100;
```

## 애플리케이션 전환하기

데이터 검증이 완료되면 이제 애플리케이션을 전환할 차례입니다.

### 전환 전 체크리스트

- CDC Lag이 0에 가까운지 확인 (완전 동기화)
- 주요 테이블 행 수 일치 확인
- 타겟 DB 성능 테스트 완료
- 롤백 계획 수립
- 전환 시간대 결정 (트래픽이 적은 시간)

### 전환 절차

1. <b>애플리케이션 쓰기 중단</b> — Read-Only 모드 전환
2. <b>CDC Lag = 0 대기</b> — 완전 동기화 확인
3. <b>최종 검증</b> — 주요 테이블 행 수 비교
4. <b>DB 엔드포인트 변경</b> — 타겟 DB로 변경
5. <b>애플리케이션 재시작</b>
6. <b>정상 동작 확인</b>

### 전환 후

- 24\~48시간 모니터링
- DMS Task 중지
- 소스 DB 1\~2주 유지 (롤백 대비)
- 이상이 없으면 소스 DB 삭제

<b>주의:</b> DMS는 단방향 복제만 지원합니다. 전환 후 소스 DB를 사용하면 안 됩니다.

## 주의사항과 팁

### Binlog Position 타이밍

다시 한번 강조하지만, binlog position은 스냅샷 생성 전에 확인해야 합니다. 이 타이밍을 놓치면 데이터 정합성 문제가 발생할 수 있습니다.

### Binlog Retention 충분히 확보

Binlog retention은 충분히 길게 설정하세요. 스냅샷 생성, 공유, 복사, 복원까지 예상보다 시간이 오래 소요됩니다. 48시간 이상을 권장합니다.

### 커넥션 타임아웃 대응

소스 DB에 변경사항이 없는 시간이 길어지면 타겟 DB 연결이 중단될 수 있습니다. 타겟 DB의 wait\_timeout을 24시간(86400초)으로 늘려 두는 것을 권장합니다.

```sql
SET GLOBAL wait_timeout = 86400;
```

또는 파라미터 그룹에서 영구적으로 설정할 수 있습니다.

### Primary Key 필수

DMS는 Primary Key가 없는 테이블을 정상적으로 복제하지 못할 수 있습니다. 모든 테이블에 Primary Key가 있는지 확인하세요.

```sql
-- Primary Key 없는 테이블 찾기
SELECT 
  t.TABLE_SCHEMA,
  t.TABLE_NAME
FROM information_schema.TABLES t
LEFT JOIN information_schema.KEY_COLUMN_USAGE k 
  ON t.TABLE_SCHEMA = k.TABLE_SCHEMA 
  AND t.TABLE_NAME = k.TABLE_NAME 
  AND k.CONSTRAINT_NAME = 'PRIMARY'
WHERE t.TABLE_SCHEMA NOT IN ('mysql', 'information_schema', 'performance_schema')
  AND k.COLUMN_NAME IS NULL
  AND t.TABLE_TYPE = 'BASE TABLE';
```

### Full Load + CDC와 비교

스냅샷 + CDC Only 방식을 사용하면서 느낀 점은, 확실히 시간은 절약되지만 고려해야 할 부분이 많다는 것입니다.

<b>Full Load + CDC 방식</b>을 사용하면:

- Binlog position을 DMS가 자동으로 관리
- 데이터 정합성이 자동으로 보장됨
- 운영이 훨씬 단순함

데이터 크기가 500GB 이하이거나, 데이터 정합성이 매우 중요한 경우라면 Full Load + CDC 방식을 권장합니다. 몇 시간이 더 소요되더라도 안전한 방식이 바람직합니다.

## 마치며

AWS 계정 간 RDS 마이그레이션은 예상보다 복잡한 작업입니다. 특히 스냅샷 방식을 사용한다면 binlog position 타이밍과 retention 관리가 매우 중요합니다.

이 글이 동일한 작업을 진행하시는 분들에게 도움이 되기를 바랍니다. 특히 타겟 DB 권한 설정이나 binlog position 타이밍 같은 부분은 문서에 자세히 기술되어 있지 않아 시행착오를 겪기 쉬운 부분입니다.

### 참고 자료

- [AWS DMS User Guide](https://docs.aws.amazon.com/dms/latest/userguide/)
- [Using MySQL as a Source for AWS DMS](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.MySQL.html)
- [RDS Binary Logging](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_LogAccess.MySQL.BinaryFormat.html)
