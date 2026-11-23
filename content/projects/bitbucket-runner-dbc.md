---
title: Bitbucket Self-Hosted Runner로 멀티플랫폼 빌드부터 캐시까지 - DBC 제거
summary: Self-hosted Runner에 멀티플랫폼 빌드와 BuildKit S3 캐시를 구성해 Docker Build Cloud 의존성을 제거하고, 재빌드 파이프라인을 약 1분 25초에서 1분으로 단축
field: 자동화
competency: k8s
stack: Bitbucket Pipelines, BuildKit, S3
order: 3
original: https://medium.com/midastech/bitbucket-self-hosted-runner-%ED%95%98%EB%82%98%EB%A1%9C-%EB%A9%80%ED%8B%B0%ED%94%8C%EB%9E%AB%ED%8F%BC-%EB%B9%8C%EB%93%9C%EB%B6%80%ED%84%B0-%EC%BA%90%EC%8B%9C%EA%B9%8C%EC%A7%80-dbc-%EA%B1%B7%EC%96%B4%EB%82%B4%EA%B8%B0-034ca7a2ce77
---
Bitbucket Self-hosted Runner 환경에서 Docker Build Cloud 의존성을 제거하고, BuildKit S3 캐시와 Forge 앱 기반 Dynamic Pipeline으로 멀티 Workspace 빌드/배포 파이프라인을 구성한 경험을 공유합니다.

### 왜 이 작업이 필요했나?

저희 팀은 Bitbucket Pipelines로 Docker 이미지 빌드와 배포를 관리하고 있습니다. 기존에는 A 법인(마이다스인)의 제품들만 파이프라인을 운영하고 있었고, 빌드 인프라는 다음과 같이 구성되어 있었습니다.

- <b>Self-hosted Runner</b>: EKS 클러스터에 ARM64 Runner를 생성해 빌드 실행
- <b>Docker Build Cloud(DBC)</b>: 단일/멀티 플랫폼 구분 없이 대부분의 빌드에 사용. DinD 환경에서는 캐시가 매번 소멸되기 때문에 DBC의 로컬 볼륨 캐시에 의존하고 있었음
- <b>Dynamic Pipeline</b>: Forge 앱으로 파이프라인 설정을 동적으로 주입

이 구성은 잘 동작하고 있었습니다. 그런데 B 법인(마이다스아이티)의 제품들에도 파이프라인을 적용해야 하는 상황이 발생했습니다.

### 왜 Self-hosted Runner가 필요한가?

Bitbucket Cloud의 기본 빌드 환경은 IP를 특정할 수 없습니다. 정확히는 Premium 플랜(4x 이상)에서만 고정 IP 범위를 제공합니다.

문제는 빌드 과정에서 Nexus 같은 Private 저장소에 접근해야 하는데, 이런 자산들은 IP 기반 접근 통제가 적용되어 있습니다. B 법인은 이 문제를 4x 플랜으로 우회하고 있었지만, Self-hosted Runner를 사용하면 Runner Pod의 IP가 VPC 내부로 고정되므로 이 문제가 근본적으로 해결됩니다.

### 기존 인프라를 공유할 수 없었던 이유

단순히 A 법인의 Runner 인프라를 공유하면 될 것으로 보이지만, 법인 간 비용 정산 문제로 기존 EKS 클러스터를 공유할 수 없었습니다. B 법인의 자체 EKS 클러스터에 Runner를 새로 구축해야 했습니다.

Docker Build Cloud도 마찬가지입니다. 새로운 Builder를 등록해야 하고, 계정 설정이 번거롭습니다.

### 결정적인 문제: 아키텍처 불일치

A 법인의 Runner는 <b>ARM64로만</b> 구성되어 있었고, 단일 플랫폼이든 멀티 플랫폼이든 대부분의 빌드를 Docker Build Cloud에서 처리하고 있었습니다. DinD 환경의 캐시 소멸 문제 때문에 DBC의 캐시에 의존할 수밖에 없었기 때문입니다.

그런데 B 법인은 <b>AMD64 아키텍처가 과반수 이상</b>이었습니다. 멀티 플랫폼 빌드가 필수인데, Docker Build Cloud를 사용하기도 까다로운 상황. 결국 Runner 자체에서 멀티 플랫폼을 지원하는 방향으로 결정했습니다.

Docker Build Cloud를 제거하려고 보니 또 다른 문제가 있었습니다. <b>Docker layer 캐시</b>입니다. DBC는 자체 로컬 볼륨에 캐시를 유지하지만, Self-hosted Runner는 DinD(Docker in Docker) 환경이라 <b>Pod이 종료되면 캐시가 전부 소멸됩니다</b>.

정리하면 해결해야 할 문제는 다음과 같습니다.

1. Runner에서 ARM/AMD 멀티 플랫폼 빌드 지원
2. DinD 환경의 Docker layer 캐시 소멸 문제 해결
3. 여러 Workspace에 파이프라인 설정을 효율적으로 배포

![](https://cdn-images-1.medium.com/max/1024/1*8HyHj4JGd_M5lfSzxIUDag.png)

## Runner에서 멀티 플랫폼 빌드 지원하기

### 기존 구조의 한계

저희 Runner는 Bitbucket의 공식 [runners-autoscaler](https://bitbucket.org/atlassian/runners-autoscaler)를 사용합니다. 이 오토스케일러는 groups 설정에 따라 Runner Job을 K8s에 생성하는데, 기존에는 ARM64 그룹만 있었습니다.

```yaml
groups:
  - name: "arm64-runner"
    workspace: "my-workspace"
    labels: ["pr.devops.arm64"]
    architecture: "arm64"
    node_selector: {}
    # ...
```

여기에 AMD64 그룹을 추가하면 됩니다. 그런데 문제가 하나 있었습니다.

### Node Affinity 하드코딩 문제

Runner Job의 Pod template에서 <code>kubernetes.io/arch</code>가 arm64로 <b>하드코딩</b>되어 있었습니다.

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/arch
              operator: In
              values:
                - "arm64"    # 하드코딩!
```

이 값을 그룹별로 다르게 지정해야 합니다.

### 첫 번째 시도: runners-autoscaler 코드를 직접 수정하다 되돌리기까지

처음에는 runners-autoscaler 코드에 architecture 필드를 Job template까지 전달하도록 수정했습니다. KubernetesServiceData에 필드를 추가하고, Scaling 로직에서 값을 전달하고, 테스트 코드도 수정했습니다. 총 6개 파일을 변경했습니다.

그런데 한 가지 간과한 점이 있었습니다. runners-autoscaler는 <b>Atlassian 공식 오픈소스를 clone</b>해서 사용하고 있었습니다. 새 버전이 릴리스될 때마다 수정 사항을 다시 적용해야 하고, 충돌이 발생하면 머지도 해야 합니다. 6개 파일 수정은 유지보수 부담이 지나치게 컸습니다.

결국 모든 변경을 <b>전부 되돌렸습니다</b>.

### 두 번째 시도: 코드 한 줄 수정하지 않고 해결하기

runners-autoscaler의 코드를 다시 분석해보니, node\_selector 필드가 이미 Groups 설정에서 Job template까지 전달되고 있었습니다. 다만 template에서 사용하지 않고 있었을 뿐이었습니다.

```txt
GroupData(validators.py)
  -> KubernetesServiceData(kubernetes.py)
  -> Jinja2 template 렌더링
```

Job template은 Jinja2로 렌더링되므로, 하드코딩된 값을 다음과 같이 변경하면 됩니다.

```txt
# Before
- "arm64"

# After
- "<%node_selector.get('kubernetes.io/arch', 'arm64')%>"
```

node\_selector에 값이 없으면 기본값 arm64를 사용하고, 값이 있으면 해당 아키텍처를 사용합니다. <b>runners-autoscaler 코드는 한 줄도 수정하지 않았습니다.</b>

이제 runners\_config.yaml에 AMD64 그룹을 추가할 수 있습니다.

```yaml
groups:
  - name: "arm64-runner"
    labels: ["pr.devops.arm64"]
    architecture: "arm64"
    node_selector: {}                                      # 기본값 arm64
    # ...

  - name: "amd64-runner"
      labels: ["pr.devops.amd64"]
      architecture: "amd64"
      node_selector: {"kubernetes.io/arch": "amd64"}         # amd64 지정
      # ...
```

Karpenter NodePool도 runner-arm64, runner-amd64 두 개를 준비하고, Job template의 matchExpressions에 양쪽 NodePool을 모두 포함시켰습니다. arch affinity가 자동으로 올바른 노드에 스케줄링합니다.

## Docker Layer 캐시 원격 저장

### DinD 환경의 캐시 문제

Self-hosted Runner는 Docker in Docker(DinD) 사이드카 패턴으로 동작합니다. Runner 컨테이너 옆에 docker:dind 컨테이너가 함께 실행되어 Docker 데몬 역할을 합니다.

문제는 <b>DinD 컨테이너의</b> <b>/var/lib/docker가 emptyDir 볼륨</b>이라는 것입니다. 파이프라인이 완료되면 Pod이 삭제되고, 캐시도 함께 소멸됩니다. 매번 모든 이미지 레이어를 처음부터 빌드해야 합니다.

Docker Build Cloud를 사용했을 때는 DBC 서버가 자체 로컬 볼륨에 캐시를 유지했기 때문에 이 문제를 고려하지 않아도 됐습니다.

### 첫 번째 아이디어: EFS로 캐시를 공유하면 안 될까?

DinD의 캐시가 소멸되는 것이 문제라면, /var/lib/docker를 영속 볼륨에 마운트하면 되지 않을까? 가장 먼저 검토한 아이디어는 EFS(Elastic File System)를 마운트해서 여러 Runner Pod이 Docker 캐시를 공유하는 것이었습니다.

결론부터 말하면 <b>불가능</b>합니다. 두 가지 이유가 있습니다.

1. <b>Docker 데몬은</b> <b>/var/lib/docker에 독점적으로 접근한다는 전제로 설계</b>되어 있습니다. 여러 DinD 컨테이너가 같은 경로를 공유하면 파일시스템과 이미지가 손상될 수 있습니다. DinD의 원 개발자인 Jerome Petazzoni도 이를 명확히 경고합니다.
2. <b>모든 Docker I/O가 네트워크 파일시스템을 경유</b>합니다. 캐시가 hit해도 빌드 자체가 느려집니다. 그렇다고 Pod별 별도 EFS 경로를 사용하면 캐시 공유가 불가능하므로 의미가 없습니다.
3. 결국 로컬 볼륨이 아닌 <b>빌드 도구 레벨에서 원격 캐시를 관리</b>하는 방향으로 전환했습니다.

### 두 번째 시도: ECR Registry 캐시와 Tag Immutability의 충돌

Docker BuildKit(buildx)은 빌드 캐시를 외부 저장소에 저장하는 기능을 제공합니다. --cache-from과 --cache-to 옵션으로 캐시를 S3나 ECR에 저장할 수 있습니다.

가장 자연스러운 선택은 이미 사용 중인 ECR을 캐시 저장소로 사용하는 것이었습니다.

```bash
docker buildx build \
  --cache-from type=registry,ref=$ECR_REPO:cache \
  --cache-to type=registry,ref=$ECR_REPO:cache,mode=max \
  -t $ECR_REPO:$TAG --push .
```

그런데 <b>ECR Tag Immutability</b>와 충돌했습니다. cache 태그를 매 빌드마다 덮어써야 하는데, Immutable로 설정된 레포에서는 push가 실패합니다. ECR에서 개별 레포 단위로 exclusion 설정이 가능하긴 하지만, 새 레포가 생성될 때마다 수동으로 적용해야 합니다.

### 세 번째 시도: S3 캐시 백엔드로 문제 해결

ECR을 사용할 수 없으므로 다른 저장소를 찾아야 했습니다. BuildKit은 S3 백엔드도 지원합니다.

```bash
docker buildx build \
  --cache-from type=s3,region=ap-northeast-2,bucket=$CACHE_BUCKET,name=$IMAGE_NAME \
  --cache-to type=s3,region=ap-northeast-2,bucket=$CACHE_BUCKET,name=$IMAGE_NAME,mode=max \
  -t $IMAGE_URL --push --provenance=false .
```

S3는 태그 개념이 없으므로 Immutability 문제가 원천적으로 없습니다. 하나의 버킷에서 name 파라미터로 프로젝트별 캐시를 분리할 수 있고, lifecycle rule로 오래된 캐시를 자동 정리하기도 용이합니다. Pipelines dependency 캐시용으로 이미 생성해 둔 S3 버킷을 그대로 재활용할 수 있다는 점도 장점이었습니다.

### 빌드 스크립트 수정

기존 docker build + docker tag + docker push 3단계를 docker buildx build --push 하나로 통합했습니다.

```bash
# Buildx 설치
mkdir -vp ~/.docker/cli-plugins/
BUILDX_URL="https://github.com/docker/buildx-desktop/releases/download/v0.19.2-desktop.1/buildx-v0.19.2-desktop.1.linux-$BASE_PLATFORM"
curl --silent -L --output ~/.docker/cli-plugins/docker-buildx "$BUILDX_URL"
chmod a+x ~/.docker/cli-plugins/docker-buildx

# Builder 생성 (docker-container 드라이버)
docker buildx create --use --name runner-builder --driver docker-container 2>/dev/null \
  || docker buildx use runner-builder
# S3 캐시 옵션 (PIPELINE_CACHE_S3_BUCKET 변수가 있을 때만 활성화)
S3_CACHE_OPTION=""
if [ -n "$PIPELINE_CACHE_S3_BUCKET" ]; then
  S3_CACHE_OPTION="--cache-from type=s3,region=${AWS_DEFAULT_REGION},bucket=${PIPELINE_CACHE_S3_BUCKET},name=${ECR_IMAGE_NAME} \
    --cache-to type=s3,region=${AWS_DEFAULT_REGION},bucket=${PIPELINE_CACHE_S3_BUCKET},name=${ECR_IMAGE_NAME},mode=max"
fi
# 빌드 + 푸시
docker buildx build $S3_CACHE_OPTION \
  --tag $IMAGE_URL --push --provenance=false .
```

--provenance=false는 ECR 호환성을 위해 필요합니다. BuildKit은 기본적으로 provenance manifest를 생성하는데, 일부 ECR 설정에서 문제를 일으킬 수 있습니다.

### 예상 밖의 결과: S3 Cache vs Docker Build Cloud

S3 캐시로 전환하면서 한 가지 우려가 있었습니다. Docker Build Cloud는 고성능 빌더에 로컬 볼륨 캐시를 사용하므로 빌드 자체는 분명히 빠를 것이고, S3 캐시는 매번 네트워크를 통해 캐시를 주고받아야 하므로 느리지 않을까?

같은 이미지를 변경 없이 재빌드해서 비교했습니다.

<b>S3 Cache (Runner) vs Docker Build Cloud</b>

- BuildKit 부트스트랩: 5.3s vs 0.0s (상시 가동)
- 캐시 로드: 0.1s (S3) vs 0.0s (로컬 볼륨)
- <b>ECR push: \~0s (같은 리전) vs 5.7s (클라우드 -> 서울)</b>
- <b>전체 파이프라인: \~1분 vs \~1분 25초</b>

결과는 예상과 정반대였습니다. S3 Cache 방식이 <b>더 빨랐습니다</b>. 이유는 간단하지만, 직접 측정하기 전까지는 파악하기 어려운 부분이었습니다. <b>ECR push 위치</b>가 달랐던 것입니다.

- S3 Cache: Runner가 같은 리전(ap-northeast-2) VPC 내에서 빌드하고 ECR로 push -> 거의 즉시
- DBC: Docker Cloud(해외)에서 빌드하고 서울 ECR로 크로스리전 push -> 5.7초

100MB 수준의 작은 이미지에서도 25초 차이가 발생했습니다. <b>이미지가 커질수록 격차가 벌어집니다</b>. 1GB 이미지라면 S3 Cache가 2배 이상 빠를 것으로 예상합니다.

Docker Build Cloud를 제거하면서 오히려 빌드가 빨라질 것이라고는 예상하지 못했습니다. DBC의 고성능 빌더보다 <b>네트워크 지연이 더 큰 병목</b>이었던 셈입니다. “빌드 속도”만 보면 DBC가 유리할 수 있지만, 최종 이미지가 도착해야 할 곳(ECR)이 한국 리전이라면 같은 리전에서 빌드하는 것이 전체 파이프라인 기준으로는 더 빠릅니다.

## Pipelines Dependency 캐시 원격 저장

Docker layer 캐시와는 별개로, <b>Pipelines dependency 캐시</b>(node\_modules, .m2, .gradle 등)도 문제입니다. DinD 환경에서는 이것도 Pod과 함께 소멸되기 때문입니다.

### Runner v5 Custom Storage Provider

Bitbucket Runner v5에서 Custom Storage Provider 기능이 추가되었습니다. Self-hosted Runner가 캐시를 S3나 GCS에 저장할 수 있도록 지원하는 기능입니다.

bitbucket-pipelines.yml에 다음과 같이 설정합니다.

```yaml
definitions:
  caches:
    node: node_modules
pipelines:
  custom:
    build:
      - step:
          name: Build
          caches:
            - node
          script:
            - npm ci
            - npm run build
          runtime:
            self-hosted:
              storage:
                aws:
                  bucket: my-cache-bucket
                  region: ap-northeast-2
                  oidc-role: arn:aws:iam::123456789:role/my-role
```

주의할 점이 있습니다.

- 이 기능은 <b>Docker layer 캐시가 아닌 Pipelines dependency 캐시</b>를 저장합니다. caches: 에 정의된 디렉토리를 S3에 업로드/다운로드하는 것입니다.
- <code>runtime.self-hosted.storage</code> 설정은 bitbucket-pipelines.yml에 정의하지만, <b>실제 캐시 처리는 Runner가 수행</b>합니다. 따라서 Runner v5 이상이 필요합니다.
- Bitbucket Cloud의 기본 러너는 Atlassian 캐시 저장소를 자동으로 사용하므로 이 설정이 불필요합니다. <b>Self-hosted Runner 전용</b> 기능입니다.

## Dynamic Pipeline으로 멀티 Workspace 배포 자동화

### 기존 Forge 앱에 기능 추가

저희는 이미 Bitbucket의 [Dynamic Pipelines](https://developer.atlassian.com/cloud/bitbucket/dynamic-pipelines/)를 활용한 Forge 앱을 운영하고 있었습니다. 파이프라인 실행 시점에 OIDC 설정과 Runner 레이블을 자동 주입하는 역할이었습니다.

이번에 멀티 플랫폼과 S3 캐시가 추가되면서, 이 설정들을 수십 개 레포의 bitbucket-pipelines.yml에 일일이 추가하는 것은 비현실적이었습니다. 기존 Forge 앱을 확장해서 새로운 설정도 자동 주입하도록 했습니다:

1. <b>모든 스텝에</b> <b>oidc: true 주입</b> — AWS OIDC 인증을 위해 (기존)
2. <b>RUNNER\_STEPS에 해당하는 스텝에 Runner 설정 주입</b> — runs-on 레이블 (기존)
3. <b>Runner 스텝에 빌드 플랫폼 설정 주입</b> — BASE\_PLATFORM에 따른 아키텍처 선택 (신규)
4. <b>Runner 스텝에 S3 캐시 스토리지 자동 주입</b> — <code>runtime.self-hosted.storage</code> (신규)
5. <b>스텝별</b> <b>max-time 주입</b> — 타임아웃 관리 (기존)

### S3 캐시 스토리지 자동 주입

Forge 앱은 Workspace/Repository 변수를 조회해서, PIPELINE\_CACHE\_S3\_BUCKET과 AWS\_ROLE\_ARN이 모두 존재할 때만 S3 스토리지를 주입합니다.

```javascript
const injectRunner = (step) => {
// ... Runner 설정 주입 ...
// S3 캐시 스토리지 주입
  const cacheBucketVar = repoVariables.find(v => v.key === "PIPELINE_CACHE_S3_BUCKET");
  const roleArnVar = repoVariables.find(v => v.key === "AWS_ROLE_ARN");
  if (cacheBucketVar && roleArnVar) {
    updatedStep.runtime = {
      ...updatedStep.runtime,
      "self-hosted": {
        ...updatedStep.runtime?.["self-hosted"],
        storage: {
          aws: {
            bucket: cacheBucketVar.value,
            region: "ap-northeast-2",
            "oidc-role": roleArnVar.value,
          },
        },
      },
    };
  }
  return updatedStep;
};
```

이렇게 하면 각 레포의 bitbucket-pipelines.yml에는 비즈니스 로직(빌드, 테스트, 배포)만 남기고, 인프라 설정은 Forge 앱이 일괄 관리합니다.

새로운 Workspace를 추가할 때도 Forge 앱을 설치하고 변수만 설정하면 완료됩니다.

### 플랫폼 선택

BASE\_PLATFORM 변수로 레포지토리별 빌드 플랫폼을 선택할 수 있습니다.

```javascript
const BASE_PLATFORM = basePlatformVar ? basePlatformVar.value : "arm64";  // 기본값 arm64
const linuxLabel = BASE_PLATFORM === "arm64" ? "linux.arm64" : "linux";
const runsOnValues = ["self.hosted", linuxLabel, `devops.${BASE_PLATFORM}`];
```

B 법인처럼 AMD64가 주력인 Workspace는 BASE\_PLATFORM=amd64로 설정하면 됩니다.

## S3 버킷 권한: 멀티 계정 접근

Docker layer 캐시와 Pipelines dependency 캐시 모두 같은 S3 버킷을 사용합니다. 여러 AWS 계정에서 접근해야 하므로 bucket policy에 Principal을 복수로 지정합니다.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": [
          "arn:aws:iam::111111111111:root",
          "arn:aws:iam::222222222222:root"
        ]
      },
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::my-cache-bucket",
        "arn:aws:s3:::my-cache-bucket/*"
      ]
    }
  ]
}
```

## 정리

<b>Runner ARM 단일 아키텍처 -></b> runners-autoscaler에 AMD64 그룹 추가 + Job template node affinity 변수화 (autoscaler 코드 변경 0건)

<b>DinD Docker layer 캐시 소멸</b> -> BuildKit + S3 캐시 백엔드 (--cache-from/to type=s3)

<b>DinD Pipelines dependency 캐시 소멸</b> -> Runner v5 Custom Storage Provider (S3)

<b>멀티 Workspace 설정 관리</b> -> Forge 앱 Dynamic Pipeline으로 Runner/OIDC/캐시 설정 자동 주입

<b>Docker Build Cloud 의존성</b> -> Runner 자체 빌드로 대체. 같은 리전 빌드+push로 오히려 성능 향상

Docker Build Cloud를 제거하면서 오히려 빌드 성능이 개선된 것은 의외의 결과였습니다. 핵심은 “빌드와 ECR이 같은 리전에 있느냐”였습니다. DBC의 고성능 빌더보다 네트워크 지연이 더 큰 병목이었던 셈입니다.

DinD 환경의 캐시 문제도 S3 원격 캐시로 충분히 해결할 수 있었습니다. 완벽하지는 않지만(첫 빌드는 여전히 캐시 미스), 두 번째 빌드부터는 캐시 hit으로 빌드 시간이 크게 단축됩니다.

이번 작업을 진행하면서 가장 중요하게 고려한 것은 <b>유지보수</b>였습니다. runners-autoscaler 코드를 6개 파일이나 수정했다가 전부 되돌린 것도, Forge 앱으로 인프라 설정을 중앙 관리하는 것도, 결국 같은 이유입니다. 관리 포인트가 파편화되면 당장은 이상이 없더라도 향후 반드시 문제가 됩니다. 누군가 이 시스템을 인계받았을 때 흐름을 예측할 수 있는 구조인지, 새로운 Workspace나 레포가 추가될 때 수정해야 할 곳이 최소화되는지를 기준으로 판단했습니다.

한편으로는 한계도 분명히 느꼈습니다. 아무리 Runner를 자체 클러스터에 배포해도, 파이프라인 트리거와 오케스트레이션은 여전히 Bitbucket Cloud에 의존하고 있습니다. 실제로 Bitbucket Pipelines 장애 시 빌드 자체를 실행할 수 없는 상황이 발생합니다. 이번에는 기존 플랫폼 위에서 할 수 있는 최선을 다했지만, 장기적으로는 빌드 파이프라인 자체를 우리가 통제할 수 있는 환경으로 이전해야 한다는 필요성을 느꼈습니다.

## 참고 자료

- [Bitbucket Pipelines Runner — Custom Storage Provider](https://support.atlassian.com/bitbucket-cloud/docs/set-up-custom-storage-for-build-caches/)
- [Docker BuildKit S3 Cache Backend](https://docs.docker.com/build/cache/backends/s3/)
- [Bitbucket Dynamic Pipelines](https://developer.atlassian.com/cloud/bitbucket/dynamic-pipelines/)
- [runners-autoscaler (Atlassian)](https://bitbucket.org/atlassian/runners-autoscaler)
