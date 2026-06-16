# 최종 PDR: Docking-to-MD Web Platform for Laboratory Use

**문서 버전:** v1.2 Final  
**작성 목적:** 연구실 내부용 docking 결과 기반 MD 시뮬레이션 자동화 웹 플랫폼 구축 요구사항 정의  
**기본 접속 포트:** 8888  
**초기 계정:** ID `csbl` / PW `csbl`  
**표준 MD 엔진:** GROMACS  
**표준 MD 조건:** 50 ns 기본값, 10/50/100 ns preset 제공  

---

## 1. 문서 개요

본 문서는 연구실에서 사용할 **Docking 결과 기반 MD 시뮬레이션 자동화 웹 플랫폼** 구축을 위한 Product Design Requirements, PDR 최종본이다. 플랫폼의 목적은 AutoDock Vina 등에서 생성된 docking 결과를 입력받아, 표준화된 MD 시뮬레이션 환경에서 자동으로 전처리, MD 실행, 결과 분석, 시각화, 다운로드까지 수행하는 것이다.

본 플랫폼은 연구실 내부 서버에서 우선 운영하며, 추후 다른 서버에도 쉽게 설치할 수 있도록 Docker 기반으로 배포 가능하게 설계한다.

### 1.1 v1.2 Final 반영 사항

- MD 엔진을 **GROMACS로 고정**한다.
- small molecule ligand의 표준 force field 툴체인은 **AMBER ff14SB / GAFF2 / AM1-BCC / ACPYPE** 조합으로 고정한다.
- PDBQT 파일은 **docking pose 좌표 소스**로만 사용한다.
- ligand의 bond order, formal charge, protonation/tautomer state는 PDBQT에서 추정하지 않고 **SDF/MOL2 기반 chemistry 정의 파일**에서 가져온다.
- SMILES 입력은 허용하되, pose 좌표와 atom mapping이 검증되는 경우에만 허용한다.
- Meeko 기반 입력은 Meeko remark와 atom index mapping이 보존되어 docked pose SDF export가 가능한 경우에만 chemistry 정의 파일 생략을 허용한다.
- small molecule ligand, peptide ligand, protein partner를 구분하여 처리한다.
- CIF/PDB의 HETATM은 자동으로 ligand로 확정하지 않고, Review required 방식으로 사용자 확인을 요구한다.
- GPU뿐 아니라 CPU, RAM, storage, movie rendering queue까지 운영 자원 관리 대상으로 포함한다.

---

## 2. 프로젝트 목적

### 2.1 핵심 목적

사용자가 웹 브라우저에서 docking 결과 파일과 필요한 구조 파일을 업로드하면 서버가 자동으로 다음 과정을 수행한다.

1. 입력 파일 검증
2. receptor, ligand, peptide/protein, chemical 구조 분리 및 표준화
3. ligand chemistry 정의와 docking pose 좌표 간 atom mapping 검증
4. docking pose별 MD 시뮬레이션 작업 생성
5. GPU 자원 할당
6. 큐 기반 순차 또는 GPU 수 기반 병렬 실행
7. MD 결과 분석
8. trajectory, RMSD, RMSF, Rg, SASA, H-bond, energy 등 주요 결과 시각화
9. MD trajectory 영상 또는 interactive 3D viewer 제공
10. 모든 결과 파일 다운로드

### 2.2 사용 대상

- 연구실 PI
- 대학원생
- 연구원
- docking 또는 MD를 수행해야 하지만 Linux command-line 작업에 익숙하지 않은 사용자
- 다수의 docking pose를 일괄적으로 MD 검증하고 싶은 사용자

### 2.3 비목표

MVP에서는 다음 기능을 필수 범위에 포함하지 않는다.

- 완전한 cloud-native multi-tenant SaaS 기능
- SLURM/HPC cluster 연동
- MM/PBSA 또는 MM/GBSA 자동화
- covalent docking ligand 자동 parameterization
- metal complex, cofactor, modified residue의 완전 자동 parameterization
- 모든 MD engine 선택 기능

위 기능은 Phase 4 확장 항목으로 둔다.

---

## 3. 기본 접속 및 운영 조건

### 3.1 웹 접속

- 웹 서비스는 기본적으로 `http://서버IP:8888`에서 실행한다.
- 기본 포트는 `8888`로 설정한다.
- Docker 실행 시 포트 매핑은 host 8888에서 container 80 또는 8888로 연결한다.

```bash
docker compose up -d
# host 8888 -> container web service
```

### 3.2 기본 계정

초기 관리자 계정은 다음과 같이 설정한다.

- ID: `csbl`
- PW: `csbl`

단, 보안상 최초 로그인 시 반드시 비밀번호 변경을 요구한다.

### 3.3 권한 구조

| 권한 | 기능 |
|---|---|
| Admin | 사용자 관리, GPU 제어, 작업 삭제, 전체 작업 확인, 시스템 설정 변경 |
| User | 작업 등록, 본인 작업 확인, 결과 확인, 파일 다운로드 |

향후 연구실 외부 공동연구자 계정이 필요한 경우 Project 단위 권한을 추가할 수 있다.

---

## 4. 전체 시스템 개요

플랫폼은 다음 컴포넌트로 구성한다.

### 4.1 Web Frontend

- 로그인 화면
- 작업 업로드 화면
- 작업 현황 대시보드
- GPU 상태 대시보드
- 결과 시각화 화면
- 파일 다운로드 화면

### 4.2 Backend API Server

- 사용자 인증
- 파일 업로드
- 입력 파일 검증
- 작업 생성
- 큐 등록
- 작업 상태 관리
- GPU 자원 관리
- 결과 분석 데이터 제공

### 4.3 Worker

- 큐에서 작업을 가져와 MD 파이프라인 실행
- 작업당 GPU 1개 할당
- 전처리, MD 실행, 후처리, 분석, 결과 패키징 수행
- 로그와 progress를 backend에 지속적으로 업데이트

### 4.4 Queue System

- 작업 대기열 관리
- FIFO 기반 기본 실행
- Admin 우선순위 조정
- 실패 작업 재시도 또는 중단 처리
- pose별 sub-job 관리

### 4.5 Database

- 사용자 정보
- 작업 메타데이터
- 파일 경로
- 작업 상태
- GPU 할당 로그
- 분석 결과 인덱스
- 저장공간 사용량

### 4.6 File Storage

- 원본 입력 파일
- 중간 전처리 파일
- MD trajectory
- 분석 결과
- 영상 파일
- 압축 다운로드 파일

### 4.7 MD Engine Container

- GROMACS GPU 빌드
- AmberTools antechamber
- ACPYPE
- RDKit
- MDAnalysis 또는 MDTraj
- visualization 변환 도구
- movie rendering 도구

---

## 5. 권장 기술 스택

### 5.1 Frontend

- React 또는 Next.js
- TypeScript
- Tailwind CSS
- Plotly.js 또는 ECharts
- NGL Viewer 또는 Mol* Viewer
- WebSocket 또는 Server-Sent Events 기반 실시간 상태 업데이트

### 5.2 Backend

- Python FastAPI
- SQLAlchemy
- PostgreSQL 또는 SQLite
- Redis
- Celery 또는 RQ
- WebSocket 또는 SSE

초기 연구실 내부 버전에서는 SQLite + Redis + RQ 조합으로 시작할 수 있다. 장기 운영 및 다중 사용자 환경을 고려하면 PostgreSQL + Redis + Celery 구조가 더 안정적이다.

### 5.3 MD Execution

**표준 MD 엔진:** GROMACS  
**OpenMM backend:** Phase 4 확장 항목으로 보류

### 5.4 Force Field 및 Parameterization 표준

| 대상 | 표준 설정 |
|---|---|
| Protein receptor | AMBER ff14SB |
| Small molecule ligand | GAFF2 |
| Small molecule partial charge | AM1-BCC |
| Ligand topology 변환 | antechamber + ACPYPE |
| Water model | TIP3P |
| MD engine | GROMACS |

### 5.5 적용 범위 제한

위 자동 parameterization은 **일반적인 organic small molecule ligand**를 1차 대상으로 한다. 다음 경우에는 자동화 실패 가능성이 높으므로 예외 처리 또는 수동 parameter 업로드 기능을 둔다.

- metal complex
- covalent inhibitor
- boron, silicon 등 비표준 원소 포함 화합물
- cofactor
- modified residue
- glycosylation
- peptide-like ligand
- non-standard peptide
- protein-protein complex 중 비표준 residue 포함 사례

---

## 6. 입력 파일 및 화학 정보 처리 원칙

### 6.1 기본 원칙: 좌표는 pose에서, chemistry는 정의 파일에서

MD 시작 좌표는 docking pose에서 가져오되, ligand의 화학적 정의는 별도의 신뢰 가능한 chemistry 정의 파일에서 가져온다.

AutoDock Vina의 PDBQT 포맷은 AutoDock atom type과 charge 중심이며, 일반적으로 bond order, formal charge, protonation/tautomer state를 MD parameterization에 충분한 수준으로 보존하지 않는다. 따라서 PDBQT 좌표만으로 bond order를 역추정하면 aromatic ring, conjugation, carboxylate, phosphate, charged amine 등에서 오류가 발생하기 쉽다.

따라서 본 플랫폼은 다음 원칙을 따른다.

- PDBQT는 docking pose의 **3D 좌표 소스**로만 사용한다.
- ligand chemistry는 SDF/MOL2를 기본 권장 입력으로 한다.
- SMILES는 protonation/tautomer가 확정되어 있고 atom mapping이 성공하는 경우에만 허용한다.
- bond order와 formal charge는 PDBQT에서 perception하지 않는다.
- chemistry 정의와 pose 좌표의 원자 구성이 일치하지 않으면 작업 생성을 거부한다.

### 6.2 필수 입력 파일

각 docking-to-MD 작업은 다음 입력을 기본으로 요구한다.

#### 1. Ligand docking pose file

- AutoDock Vina 결과 PDBQT 파일
- 여러 pose가 포함된 PDBQT 파일 지원
- 상위 n개 pose를 선택하여 각각 MD sub-job 생성
- 이 파일에서는 좌표만 사용한다.

#### 2. Ligand chemistry definition

기본 권장 형식:

- SDF
- MOL2

조건부 허용 형식:

- protonation/tautomer state가 확정된 isomeric SMILES
- Meeko-compatible PDBQT 또는 Meeko export SDF

SMILES 입력은 pose PDBQT와 atom mapping이 성공하는 경우에만 허용한다. Mapping 실패 시 작업 생성을 거부한다.

#### 3. Receptor structure

- PDB 또는 CIF
- 표준 residue와 protonation이 정리된 구조 권장
- CIF 입력 시 receptor 측 구조 입력으로 처리한다.

### 6.3 Meeko 사용 시 조건부 간편 경로

Meeko로 docking 전처리를 수행한 경우, Meeko가 PDBQT remark에 SMILES 및 atom index mapping 정보를 보존할 수 있다. 이 정보가 유지되어 docked pose SDF export가 가능한 경우에 한해 별도 chemistry 정의 파일 업로드를 생략할 수 있다.

단, raw PDBQT만으로 chemistry를 추정하는 것은 허용하지 않는다.

### 6.4 Small molecule / Peptide / Protein partner 처리 분기

| 입력 유형 | 권장 처리 |
|---|---|
| Small molecule ligand | GAFF2 / AM1-BCC / antechamber / ACPYPE |
| Peptide ligand | AMBER ff14SB 기반 protein/peptide parameterization |
| Protein partner | AMBER ff14SB 기반 protein-protein complex 처리 |
| Modified peptide | custom residue parameter 필요 가능 |
| Non-standard ligand | 수동 parameter 업로드 또는 실패 처리 |

작업 업로드 시 ligand type을 다음 중 하나로 지정하거나 자동 후보 분류 후 사용자 확인을 받는다.

- small molecule
- peptide
- protein partner
- cofactor
- unknown / review required

---

## 7. 작업 업로드 기능 요구사항

### 7.1 업로드 옵션

| 항목 | 설명 | 기본값 |
|---|---|---|
| Job name | 작업 이름 | 자동 생성 |
| Input type | PDBQT / CIF / PDB / mixed | 자동 감지 |
| Ligand type | small molecule / peptide / protein partner | 자동 후보 + 사용자 확인 |
| Number of top poses | 상위 n개 pose 선택 | 3 |
| MD length | MD simulation 시간 | 50 ns |
| Protein force field | 단백질 force field | AMBER ff14SB 고정 |
| Ligand force field | ligand force field | GAFF2 / AM1-BCC, small molecule 한정 |
| Water model | 물 모델 | TIP3P |
| Box type | simulation box 형태 | dodecahedron 또는 cubic |
| Salt concentration | 염 농도 | 0.15 M |
| Temperature | 온도 | 300 K |
| Pressure | 압력 | 1 atm |
| GPU use | GPU 사용 여부 | 사용 |
| Priority | 작업 우선순위 | Normal |

### 7.2 MD 길이 옵션

- 10 ns: 테스트용
- 50 ns: 기본값
- 100 ns: 정밀 분석용
- Custom: Admin 또는 advanced user만 허용

기본값은 반드시 **50 ns**로 설정한다.

### 7.3 Acceptance Criteria

- 사용자는 PDBQT 파일을 업로드할 수 있어야 한다.
- 여러 pose가 포함된 PDBQT 파일의 pose 수를 자동 감지해야 한다.
- 상위 n개 pose를 선택할 수 있어야 한다.
- 각 pose는 독립적인 MD sub-job으로 생성되어야 한다.
- raw PDBQT만 업로드된 경우 작업 생성을 거부해야 한다.
- SDF/MOL2 또는 검증 가능한 SMILES/Meeko mapping 없이 ligand parameterization을 진행하지 않아야 한다.
- chemistry 정의와 pose 좌표의 원자 구성이 일치하지 않으면 작업 시작 전에 검증 오류를 표시해야 한다.

---

## 8. PDBQT Multiple Pose 처리

### 8.1 처리 절차

1. PDBQT 파일 업로드
2. `MODEL`, `ENDMDL` 또는 score line 기준으로 pose 분리
3. 각 pose의 docking score 추출
4. score 기준 정렬
5. 상위 n개 pose 선택
6. chemistry 정의 파일과 pose 좌표 간 atom mapping 검증
7. heavy atom graph 기준으로 bond order 부여
8. formal charge, protonation state, hydrogen 처리 표준화
9. pose별 complex 생성
10. pose별 MD sub-job 생성

### 8.2 RDKit 기반 bond order 부여 시 주의사항

RDKit `AssignBondOrdersFromTemplate` 등을 사용할 경우 다음 규칙을 적용한다.

- template과 pose molecule의 hydrogen 처리 규칙을 표준화한다.
- 일반적으로 heavy atom graph를 기준으로 bond order를 부여한다.
- explicit hydrogen은 bond order 부여 후 protonation state와 formal charge를 반영하여 재구성한다.
- template과 pose 간 atom mapping이 불명확하면 자동 진행하지 않는다.
- mapping 결과를 validation report에 기록한다.

### 8.3 Pose별 디렉토리 구조

```text
jobs/
  job_20260616_001/
    input/
    pose_01/
      prep/
      md/
      analysis/
      visualization/
      results.zip
    pose_02/
      prep/
      md/
      analysis/
      visualization/
      results.zip
```

### 8.4 Acceptance Criteria

- 여러 pose가 들어 있는 PDBQT 파일을 pose별로 분리해야 한다.
- 각 pose 좌표에 chemistry 정의 파일의 bond order가 일관되게 부여되어야 한다.
- pose별 MD 상태를 독립적으로 확인할 수 있어야 한다.
- 전체 Job 단위와 pose sub-job 단위 상태를 모두 표시해야 한다.

---

## 9. CIF 입력 처리

### 9.1 요구사항

CIF 파일은 주로 receptor 측 구조 입력으로 사용한다.

처리 절차:

1. CIF 구조 파일 업로드
2. PDB 또는 MD 엔진 입력 형식으로 변환
3. chain, residue, ligand, HETATM 정보 검증
4. missing atom, alternate location, water, ion 처리 옵션 제공
5. HETATM 후보 분류
6. 사용자 확인 후 MD preparation 단계로 전달

### 9.2 HETATM 처리 정책

HETATM을 ligand로 자동 확정하지 않는다. HETATM에는 ligand뿐 아니라 water, ion, buffer, crystallization additive, cofactor, metal, glycan 등이 포함될 수 있기 때문이다.

| 옵션 | 설명 | 기본값 |
|---|---|---|
| Keep waters | 결정수 유지 여부 | False |
| Keep ions | 이온 유지 여부 | True |
| Select chain | 특정 chain만 선택 | All |
| Treat HETATM as ligand | HETATM을 ligand로 처리 | Review required |
| Cofactor handling | cofactor 유지/제거/parameter 필요 | Review required |

### 9.3 Acceptance Criteria

- CIF 파일 업로드 후 구조 검증 리포트를 생성해야 한다.
- HETATM 목록을 사용자에게 보여주고 ligand/cofactor/ion/water/additive 후보로 분류해야 한다.
- HETATM을 ligand로 처리하는 경우에도 ligand chemistry 정의 파일 또는 parameter 정보가 필요하다.
- 변환 실패 시 사용자가 확인 가능한 오류 메시지를 제공해야 한다.

---

## 10. MD Workflow 표준화

### 10.1 기본 Pipeline

각 작업은 표준화된 MD pipeline을 따른다.

1. Input validation
2. Pose parsing
3. Chemistry definition validation
4. Atom mapping validation
5. Structure preparation
6. Ligand or peptide parameterization
7. Complex assembly
8. Solvation
9. Ion addition
10. Energy minimization
11. NVT equilibration
12. NPT equilibration
13. Production MD
14. Trajectory post-processing
15. Analysis
16. Visualization generation
17. Result packaging

### 10.2 기본 MD 조건

| 항목 | 기본값 |
|---|---|
| Production MD length | 50 ns |
| Temperature | 300 K |
| Pressure | 1 atm |
| Water model | TIP3P |
| Salt concentration | 0.15 M |
| Energy minimization | 최대 50,000 steps |
| NVT equilibration | 100 ps |
| NPT equilibration | 100 ps |
| Default trajectory output | compressed XTC, 100 ps |
| Analysis trajectory option | 10–50 ps preset 선택 가능 |
| Default GPU allocation | 작업당 GPU 1개 |

### 10.3 MD 조건 Preset

#### Preset 1: Quick Test

- 10 ns
- 빠른 구조 안정성 확인
- parameterization 및 pipeline 정상 작동 확인용

#### Preset 2: Standard

- 50 ns
- 기본값
- 일반적인 docking pose 안정성 평가용

#### Preset 3: Extended

- 100 ns
- 상위 후보 pose 정밀 평가용

#### Preset 4: Custom

- Admin 또는 advanced user만 사용
- MD length, temperature, pressure, salt concentration 등 직접 설정

### 10.4 Acceptance Criteria

- 사용자가 별도 설정을 하지 않으면 50 ns MD가 실행되어야 한다.
- 모든 작업은 동일한 표준 pipeline을 따라야 한다.
- GROMACS 엔진과 고정된 force field 툴체인으로 실행되어야 한다.
- 각 단계별 로그가 저장되어야 한다.
- 실패한 단계와 오류 원인이 dashboard에 표시되어야 한다.

---

## 11. GPU 및 자원 관리

### 11.1 GPU 관리 요구사항

플랫폼은 GPU 사용 상태를 실시간으로 확인하고, 작업당 GPU 1개를 할당해야 한다.

Admin dashboard에서 다음 기능을 제공한다.

1. GPU 목록 확인
2. GPU 사용률 확인
3. GPU memory 사용량 확인
4. GPU temperature 확인
5. GPU별 실행 중인 작업 확인
6. GPU enable/disable 설정
7. 특정 GPU를 maintenance mode로 전환
8. 실행 중 작업 중단
9. 작업 재시작
10. 작업 우선순위 변경

### 11.2 GPU 할당 정책

- 하나의 MD sub-job은 하나의 GPU만 사용한다.
- 사용 가능한 GPU가 없으면 작업은 queue에서 대기한다.
- GPU가 disabled 또는 maintenance 상태이면 작업 할당 대상에서 제외한다.
- 작업이 실패하거나 중단되면 GPU lock을 해제한다.
- worker별 `CUDA_VISIBLE_DEVICES`를 고정한다.

### 11.3 GPU 상태 모델

| 상태 | 설명 |
|---|---|
| Available | 작업 할당 가능 |
| Busy | 작업 실행 중 |
| Disabled | Admin이 비활성화 |
| Maintenance | 점검 상태 |
| Error | GPU 접근 오류 또는 작업 실패 |

### 11.4 CPU, RAM, Storage 관리

GPU lock만으로는 충분하지 않다. MD preparation, trajectory analysis, movie rendering은 CPU, RAM, disk I/O를 많이 사용한다.

따라서 다음 제한을 둔다.

- worker별 CPU limit
- worker별 memory limit
- max concurrent analysis jobs
- movie rendering queue 분리
- storage quota
- temporary file cleanup
- 오래된 job archive 또는 삭제 정책

### 11.5 Acceptance Criteria

- Dashboard에서 GPU별 사용률과 memory 사용량을 확인할 수 있어야 한다.
- Admin은 특정 GPU를 사용 중지할 수 있어야 한다.
- 작업은 GPU 1개에만 할당되어야 한다.
- GPU 사용 중 충돌이 발생하지 않아야 한다.
- CPU/RAM/storage 사용량도 dashboard 또는 admin page에서 확인 가능해야 한다.

---

## 12. Queue 기반 작업 관리

### 12.1 작업 상태

| 상태 | 설명 |
|---|---|
| Uploaded | 파일 업로드 완료 |
| Validating | 입력 검증 중 |
| Queued | 큐 대기 중 |
| Preparing | MD 전처리 중 |
| Running EM | Energy minimization 실행 중 |
| Running NVT | NVT equilibration 실행 중 |
| Running NPT | NPT equilibration 실행 중 |
| Running MD | Production MD 실행 중 |
| Analyzing | 결과 분석 중 |
| Rendering | 영상 또는 viewer 파일 생성 중 |
| Packaging | 결과 압축 및 리포트 생성 중 |
| Completed | 완료 |
| Failed | 실패 |
| Cancelled | 사용자 또는 Admin이 중단 |

### 12.2 Queue 정책

- 기본은 FIFO
- Admin은 작업 우선순위를 변경할 수 있음
- GPU가 여러 개인 경우 GPU 수만큼 병렬 실행
- 작업 하나당 GPU 1개 원칙 유지
- pose별 sub-job은 개별 queue item으로 관리
- analysis와 movie rendering은 별도 queue로 분리 가능

### 12.3 ETA 표시 정책

Dashboard의 예상 종료 시간은 실행 후 측정된 ns/day 값을 기반으로 산출한다. 초기 상태에서는 정확한 ETA를 보장하지 않으며 rough estimate로 표시한다.

표시 항목:

- 현재 step
- 완료된 ns
- 전체 목표 ns
- 실측 ns/day
- rough ETA

### 12.4 Acceptance Criteria

- 사용자는 본인 작업의 queue position을 확인할 수 있어야 한다.
- Admin은 전체 queue를 확인할 수 있어야 한다.
- 작업 취소가 가능해야 한다.
- 실패한 작업은 로그와 함께 Failed 상태로 표시되어야 한다.

---

## 13. Dashboard 요구사항

### 13.1 Summary Panel

- 전체 작업 수
- 실행 중 작업 수
- 대기 중 작업 수
- 완료 작업 수
- 실패 작업 수
- 사용 가능한 GPU 수
- 사용 중 GPU 수
- storage 사용량

### 13.2 Job Queue Panel

| 항목 | 설명 |
|---|---|
| Job ID | 작업 ID |
| Job name | 작업 이름 |
| User | 사용자 |
| Input file | 입력 파일명 |
| Pose count | pose 수 |
| MD length | MD 길이 |
| Status | 상태 |
| Progress | 진행률 |
| Queue position | 큐 위치 |
| Assigned GPU | 할당 GPU |
| Completed ns | 완료된 simulation 시간 |
| ns/day | 실측 속도 |
| Rough ETA | 추정 종료 시간 |

### 13.3 GPU Panel

- GPU ID
- GPU name
- utilization
- memory usage
- temperature
- assigned job
- status
- enable/disable toggle

### 13.4 Recent Results Panel

- 최근 완료된 작업
- 주요 결과 요약
- 결과 보기
- 다운로드

### 13.5 Acceptance Criteria

- Dashboard는 최소 5–10초 간격으로 자동 갱신되어야 한다.
- 가능하면 WebSocket 또는 SSE로 실시간 업데이트를 제공한다.
- 작업 상태, GPU 상태, queue 상태, storage 상태를 한 화면에서 확인할 수 있어야 한다.

---

## 14. 작업 상세 페이지

각 작업은 상세 페이지를 가져야 한다.

### 14.1 표시 항목

#### Job metadata

- Job ID
- Job name
- 사용자
- 입력 파일
- ligand type
- chemistry source
- 생성 시간
- MD 조건
- pose 수

#### Pose별 상태

- pose index
- docking score
- MD 상태
- 할당 GPU
- 진행률
- 완료된 ns
- 결과 보기

#### 로그

- validation log
- preparation log
- MD execution log
- analysis log
- rendering log

#### 결과 시각화

- 3D structure viewer
- trajectory viewer
- RMSD plot
- RMSF plot
- radius of gyration plot
- SASA plot
- hydrogen bond plot
- energy plot
- ligand RMSD 또는 binding pose stability plot

#### 다운로드

- 전체 결과 zip
- pose별 결과 zip
- trajectory 파일
- topology 파일
- structure 파일
- analysis CSV
- graph PNG/SVG
- log 파일
- MD 영상 파일

### 14.2 Acceptance Criteria

- 사용자는 pose별 결과를 독립적으로 확인할 수 있어야 한다.
- 모든 그래프는 다운로드 가능해야 한다.
- 모든 원본 및 결과 파일은 zip으로 다운로드 가능해야 한다.

---

## 15. MD 결과 분석 및 시각화

### 15.1 기본 분석 항목

| 분석 항목 | 설명 |
|---|---|
| RMSD | protein backbone 및 ligand RMSD |
| RMSF | residue-level fluctuation |
| Radius of gyration | 전체 구조 compactness |
| SASA | solvent-accessible surface area |
| H-bond | protein-ligand hydrogen bond 수 |
| Distance analysis | ligand와 binding site 주요 residue 거리 |
| Energy analysis | potential energy, temperature, pressure 등 |
| Ligand stability | ligand pose drift, binding pocket 유지 여부 |
| Contact map | protein-ligand residue contact frequency |
| Final snapshot | 마지막 frame 구조 |

### 15.2 시각화 방식

- Plotly.js 기반 interactive graph
- PNG/SVG export
- CSV 다운로드
- pose 간 비교 plot 제공

### 15.3 Pose 비교 기능

- pose별 RMSD 비교
- pose별 ligand RMSD 비교
- pose별 H-bond occupancy 비교
- pose별 binding-site contact frequency 비교
- pose별 final structure overlay
- pose별 summary score table

### 15.4 Acceptance Criteria

- MD 완료 후 자동으로 분석 결과가 생성되어야 한다.
- 그래프는 웹에서 바로 확인 가능해야 한다.
- pose 간 비교 화면이 제공되어야 한다.

---

## 16. MD 영상 및 3D Viewer

### 16.1 Interactive 3D Viewer

- NGL Viewer 또는 Mol* Viewer 사용
- trajectory frame 재생
- protein, ligand, peptide 표시 스타일 변경
- cartoon, surface, licorice, ball-and-stick 표시
- ligand 중심 view
- frame slider 제공

### 16.2 Rendered Movie

- trajectory를 mp4 또는 webm으로 변환
- 웹에서 재생
- 다운로드 가능
- 대표 view는 ligand binding site 중심으로 자동 설정

### 16.3 Queue 정책

Movie rendering은 CPU/GPU/디스크 I/O를 많이 사용할 수 있으므로 MD execution queue와 분리할 수 있다. 기본 MVP에서는 movie rendering을 선택 기능으로 두고, trajectory viewer를 우선 제공한다.

### 16.4 Acceptance Criteria

- 사용자는 웹에서 MD trajectory를 재생할 수 있어야 한다.
- 영상 파일을 다운로드할 수 있어야 한다.
- 최소한 protein-ligand complex의 구조 변화가 확인 가능해야 한다.

---

## 17. 파일 다운로드 및 결과 패키징

### 17.1 다운로드 단위

1. 전체 Job 다운로드
2. pose별 다운로드
3. 파일 유형별 다운로드

### 17.2 포함 파일

- original input
- processed structure
- topology
- parameter files
- MD input files
- trajectory
- final structure
- log files
- analysis CSV
- graph images
- movie file
- summary report

### 17.3 결과 패키지 예시

```text
job_20260616_001_results.zip
  metadata.json
  summary_report.html
  summary_report.pdf
  input/
  pose_01/
    prep/
    md/
    analysis/
    visualization/
  pose_02/
    prep/
    md/
    analysis/
    visualization/
```

### 17.4 Acceptance Criteria

- 사용자는 전체 결과를 zip으로 다운로드할 수 있어야 한다.
- 다운로드 파일은 작업 완료 후 자동 생성되어야 한다.
- 작업 실패 시에도 로그와 중간 산출물을 다운로드할 수 있어야 한다.

---

## 18. 데이터베이스 설계 초안

### 18.1 Users

| Field | Type | 설명 |
|---|---|---|
| id | integer | 사용자 ID |
| username | string | 로그인 ID |
| password_hash | string | 암호화된 비밀번호 |
| role | string | admin/user |
| is_active | boolean | 활성 여부 |
| must_change_password | boolean | 초기 비밀번호 변경 여부 |
| created_at | datetime | 생성 시간 |

### 18.2 Jobs

| Field | Type | 설명 |
|---|---|---|
| id | string | Job ID |
| user_id | integer | 사용자 ID |
| name | string | 작업 이름 |
| input_type | string | pdbqt/cif/pdb/mixed |
| ligand_type | string | small_molecule/peptide/protein_partner/unknown |
| status | string | 작업 상태 |
| md_length_ns | integer | MD 길이 |
| top_n_poses | integer | 선택 pose 수 |
| force_field | string | protein force field |
| ligand_force_field | string | ligand force field |
| ligand_chem_source | string | sdf/mol2/smiles/meeko/manual |
| water_model | string | water model |
| salt_concentration | float | 염 농도 |
| temperature | float | 온도 |
| pressure | float | 압력 |
| created_at | datetime | 생성 시간 |
| started_at | datetime | 시작 시간 |
| completed_at | datetime | 완료 시간 |
| result_path | string | 결과 경로 |

### 18.3 SubJobs

| Field | Type | 설명 |
|---|---|---|
| id | string | Sub-job ID |
| job_id | string | 상위 Job |
| pose_index | integer | pose 번호 |
| docking_score | float | docking score |
| status | string | 상태 |
| assigned_gpu | integer | 할당 GPU |
| progress | float | 진행률 |
| completed_ns | float | 완료된 MD 시간 |
| ns_per_day | float | 실측 속도 |
| current_step | string | 현재 단계 |
| started_at | datetime | 시작 시간 |
| completed_at | datetime | 완료 시간 |
| result_path | string | pose별 결과 경로 |

### 18.4 GPUStatus

| Field | Type | 설명 |
|---|---|---|
| gpu_id | integer | GPU ID |
| name | string | GPU 이름 |
| status | string | available/busy/disabled/maintenance/error |
| utilization | float | 사용률 |
| memory_used | float | 사용 memory |
| memory_total | float | 전체 memory |
| temperature | float | 온도 |
| assigned_subjob_id | string | 현재 작업 |
| updated_at | datetime | 갱신 시간 |

### 18.5 JobLogs

| Field | Type | 설명 |
|---|---|---|
| id | integer | 로그 ID |
| job_id | string | Job ID |
| subjob_id | string | Sub-job ID |
| level | string | info/warning/error |
| step | string | pipeline step |
| message | text | 로그 메시지 |
| created_at | datetime | 로그 시간 |

### 18.6 ResourceUsage

| Field | Type | 설명 |
|---|---|---|
| id | integer | 기록 ID |
| job_id | string | Job ID |
| subjob_id | string | Sub-job ID |
| cpu_percent | float | CPU 사용률 |
| memory_used | float | memory 사용량 |
| disk_used | float | storage 사용량 |
| sampled_at | datetime | 측정 시간 |

---

## 19. API 설계 초안

### 19.1 Authentication

```text
POST /api/auth/login
POST /api/auth/logout
POST /api/auth/change-password
GET  /api/auth/me
```

### 19.2 Job

```text
POST /api/jobs
GET  /api/jobs
GET  /api/jobs/{job_id}
POST /api/jobs/{job_id}/cancel
POST /api/jobs/{job_id}/retry
DELETE /api/jobs/{job_id}
```

### 19.3 Upload

```text
POST /api/uploads/input
GET  /api/uploads/{upload_id}/validate
```

`POST /api/uploads/input`은 pose PDBQT, ligand chemistry 정의 파일, receptor 구조를 함께 받는다. `GET /api/uploads/{upload_id}/validate`는 pose 좌표와 chemistry 정의의 atom mapping 가능 여부를 검증한다.

### 19.4 Queue

```text
GET  /api/queue
POST /api/queue/{job_id}/priority
```

### 19.5 GPU

```text
GET  /api/gpus
POST /api/gpus/{gpu_id}/enable
POST /api/gpus/{gpu_id}/disable
POST /api/gpus/{gpu_id}/maintenance
```

### 19.6 Results

```text
GET /api/jobs/{job_id}/results
GET /api/jobs/{job_id}/subjobs/{subjob_id}/results
GET /api/jobs/{job_id}/download
GET /api/jobs/{job_id}/subjobs/{subjob_id}/download
GET /api/jobs/{job_id}/plots/{plot_type}
GET /api/jobs/{job_id}/trajectory
GET /api/jobs/{job_id}/movie
```

### 19.7 Realtime

```text
WS /api/ws/jobs/{job_id}
WS /api/ws/dashboard
```

또는 SSE:

```text
GET /api/events/dashboard
GET /api/events/jobs/{job_id}
```

---

## 20. 파일 구조 설계

### 20.1 Project Directory

```text
md-platform/
  docker-compose.yml
  .env.example
  backend/
    app/
    Dockerfile
  frontend/
    Dockerfile
  worker/
    Dockerfile
    pipelines/
      validate_input.py
      split_pdbqt_poses.py
      assign_bond_orders.py
      prepare_structure.py
      parameterize_ligand.py
      run_md.py
      analyze_md.py
      render_movie.py
      package_results.py
  md-env/
    Dockerfile
    forcefields/
    templates/
      gromacs/
  storage/
    jobs/
    uploads/
    results/
  scripts/
    install.sh
    backup.sh
    healthcheck.sh
```

### 20.2 Job Directory

```text
storage/jobs/{job_id}/
  metadata.json
  input/
    original/
    processed/
  pose_01/
    prep/
    md/
    analysis/
    visualization/
    logs/
    results.zip
  pose_02/
    prep/
    md/
    analysis/
    visualization/
    logs/
    results.zip
  summary/
    pose_comparison.csv
    summary_report.html
    summary_report.pdf
    all_results.zip
```

---

## 21. Docker 배포 설계

### 21.1 필수 구성

- Docker
- Docker Compose
- NVIDIA Driver
- NVIDIA Container Toolkit
- CUDA 지원 base image
- GPU별 작업 격리를 위한 `CUDA_VISIBLE_DEVICES` 제어

### 21.2 Docker Compose 서비스

필수 서비스:

1. frontend
2. backend
3. redis
4. database
5. worker-gpu-0
6. worker-gpu-1
7. worker-gpu-n
8. nginx 또는 internal reverse proxy

예시:

```yaml
services:
  frontend:
    build: ./frontend
    ports:
      - "8888:80"
    depends_on:
      - backend

  backend:
    build: ./backend
    env_file:
      - .env
    volumes:
      - ./storage:/app/storage
    depends_on:
      - redis
      - db

  redis:
    image: redis:7

  db:
    image: postgres:16
    volumes:
      - ./postgres_data:/var/lib/postgresql/data

  worker-gpu-0:
    build: ./worker
    env_file:
      - .env
    environment:
      - CUDA_VISIBLE_DEVICES=0
      - WORKER_GPU_ID=0
    volumes:
      - ./storage:/app/storage

  worker-gpu-1:
    build: ./worker
    env_file:
      - .env
    environment:
      - CUDA_VISIBLE_DEVICES=1
      - WORKER_GPU_ID=1
    volumes:
      - ./storage:/app/storage
```

실제 GPU device reservation 방식은 서버의 Docker Compose 버전과 NVIDIA Container Toolkit 설정에 맞춰 조정한다.

### 21.3 설치 목표

다른 서버에서도 다음 수준으로 설치 가능해야 한다.

```bash
git clone <repository>
cd md-platform
cp .env.example .env
docker compose up -d
```

이후 브라우저에서 다음 주소로 접속한다.

```text
http://server-ip:8888
```

### 21.4 GPU 검증 명령

서버 설치 후 다음 명령으로 Docker 내부 GPU 접근을 검증한다.

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

### 21.5 환경 변수 예시

```text
APP_PORT=8888
DEFAULT_ADMIN_ID=csbl
DEFAULT_ADMIN_PASSWORD=csbl
DEFAULT_MD_LENGTH_NS=50
DEFAULT_TOP_N_POSES=3
STORAGE_ROOT=/app/storage
DATABASE_URL=postgresql://...
REDIS_URL=redis://redis:6379/0
MAX_UPLOAD_SIZE_GB=10
GPU_ASSIGNMENT_MODE=one_job_per_gpu

MD_ENGINE=gromacs
PROTEIN_FORCE_FIELD=amber14sb
LIGAND_FORCE_FIELD=gaff2
LIGAND_CHARGE_METHOD=am1bcc
WATER_MODEL=tip3p

REQUIRE_LIGAND_CHEMISTRY=true
ALLOW_SMILES_INPUT=true
ALLOW_MEEKO_MAPPING_INPUT=true
```

---

## 22. 보안 요구사항

### 22.1 기본 보안

- 비밀번호는 plain text 저장 금지
- bcrypt 또는 argon2 기반 hash 저장
- 최초 로그인 시 기본 비밀번호 변경 강제
- 업로드 파일 확장자 및 MIME type 검증
- 작업 디렉토리 외부 경로 접근 차단
- 다운로드 경로 traversal 방지
- Admin API 권한 검증
- 실패 로그인 제한 또는 rate limit 적용
- 업로드 파일 크기 제한
- 사용자별 작업/저장공간 quota 설정

### 22.2 연구실 내부 운영 권장 사항

- 내부망에서 우선 운영
- 외부 공개 시 HTTPS 적용
- reverse proxy에서 인증 및 IP 제한 추가 가능
- 정기 백업 설정
- 사용자별 저장 용량 제한 설정

---

## 23. 오류 처리 및 로그

### 23.1 오류 유형

| 오류 유형 | 예시 |
|---|---|
| Input error | 잘못된 PDBQT/CIF 형식 |
| Chemistry mismatch | chemistry 정의와 pose 좌표 원자 구성 불일치 |
| Atom mapping error | SMILES/SDF/MOL2와 pose 간 atom mapping 실패 |
| Pose parsing error | pose 분리 실패 |
| Parameterization error | ligand parameter 생성 실패 |
| MD setup error | topology 생성 실패 |
| GPU error | GPU 접근 실패 |
| Runtime error | MD 실행 중단 |
| Analysis error | trajectory 분석 실패 |
| Storage error | 파일 저장 또는 압축 실패 |

### 23.2 사용자 오류 메시지 예시

```text
작업이 Ligand parameterization 단계에서 실패했습니다.
원인: ligand chemistry 정의 파일과 pose 좌표의 원자 구성이 일치하지 않습니다.
조치: docking에 사용한 원본 SDF/MOL2 파일이 맞는지 확인하거나, protonation state가 확정된 ligand 파일을 업로드해 주세요.
상세 로그는 아래 버튼에서 다운로드할 수 있습니다.
```

### 23.3 Acceptance Criteria

- 실패 단계, 원인, 조치 사항, 상세 로그 다운로드 링크를 제공해야 한다.
- 작업 실패 시에도 중간 산출물과 로그를 보존해야 한다.
- Admin은 전체 시스템 로그를 확인할 수 있어야 한다.

---

## 24. 성능 및 저장공간 고려사항

### 24.1 Trajectory 저장 정책

MD trajectory는 매우 큰 파일을 생성할 수 있으므로 저장 정책이 필요하다.

권장 정책:

- 기본 trajectory 저장: compressed XTC, 100 ps interval
- 분석 고해상도 preset: 10–50 ps 선택 가능
- 고해상도 저장 선택 시 storage warning 표시
- completed job 자동 압축
- 오래된 중간 파일 정리 옵션 제공
- Admin이 job별 보관 기간 설정 가능
- 기본 보관 기간: 30일 또는 연구실 정책에 따름

### 24.2 동시 작업 수

동시 실행 수는 기본적으로 GPU 수와 동일하게 제한한다.

- GPU 1개: MD 작업 1개 실행
- GPU 2개: MD 작업 2개 실행
- GPU 4개: MD 작업 4개 실행

단, pose별 sub-job도 각각 하나의 작업으로 계산한다.

---

## 25. 개발 단계별 계획

### Phase 1. MVP 구축

목표: 연구실 내부에서 기본적으로 사용할 수 있는 최소 기능 구현

기능:

1. 로그인
2. 파일 업로드
3. PDBQT pose 분리
4. SDF/MOL2 기반 chemistry 검증
5. SMILES/Meeko mapping 조건부 지원
6. ligand type 분기
7. top n pose 선택
8. 50 ns default MD 설정
9. queue 등록
10. GPU 1개당 작업 1개 실행
11. 작업 상태 표시
12. 기본 로그 표시
13. 결과 zip 다운로드

산출물:

- Docker Compose 기반 실행 환경
- Backend API
- Frontend 기본 화면
- Worker pipeline
- GROMACS MD 실행 template
- 간단한 dashboard

### Phase 2. 분석 및 시각화 고도화

목표: MD 결과를 웹에서 해석할 수 있도록 시각화 강화

기능:

1. RMSD/RMSF/Rg/SASA/H-bond 자동 분석
2. Plotly 기반 interactive graph
3. pose별 결과 비교
4. NGL Viewer 또는 Mol* Viewer 기반 trajectory viewer
5. MD movie 생성
6. summary report HTML/PDF 생성

### Phase 3. 운영 안정화

목표: 다중 사용자 및 장기 운영 가능한 수준으로 안정화

기능:

1. Admin 사용자 관리
2. GPU enable/disable
3. 작업 우선순위 변경
4. 작업 취소 및 재시작
5. 저장공간 관리
6. 자동 백업
7. 오류 로그 개선
8. health check
9. 시스템 모니터링

### Phase 4. 확장 기능

후보 기능:

1. OpenMM backend 추가
2. protein-peptide complex 특화 workflow
3. ligand protonation 상태 자동 생성
4. binding free energy 계산
5. MM/PBSA 또는 MM/GBSA 분석
6. batch job submission
7. project 단위 협업 기능
8. SLURM cluster 연동
9. cloud deployment
10. REST API 기반 외부 pipeline 연동
11. 수동 parameter 파일 업로드 기능

---

## 26. 화면 설계 초안

### 26.1 Login Page

- ID 입력
- PW 입력
- 로그인 버튼
- 최초 로그인 시 비밀번호 변경 안내

### 26.2 Job Upload Page

- 파일 업로드 영역
- pose PDBQT 업로드
- ligand chemistry 정의 파일 업로드
- receptor PDB/CIF 업로드
- ligand type 선택 또는 자동 후보 확인
- chemistry 정의와 pose atom mapping 검증 결과 표시
- top n pose 선택
- MD length 선택
- force field 확인
- water model 확인
- temperature/pressure 확인
- storage 예상 사용량 안내
- submit 버튼

### 26.3 Dashboard Page

- 작업 요약 카드
- GPU 상태 카드
- storage 상태 카드
- queue table
- running job table
- recent completed jobs

### 26.4 Job Detail Page

- job metadata
- pose별 progress
- 현재 실행 단계
- 로그 viewer
- 결과 viewer
- 다운로드 버튼

### 26.5 Result Page

- 3D viewer
- MD movie player
- RMSD plot
- RMSF plot
- H-bond plot
- energy plot
- pose comparison table
- download section

---

## 27. 우선순위

### 27.1 Must-have

- 로그인
- 기본 계정
- port 8888
- Docker 기반 실행
- PDBQT 업로드
- ligand chemistry 정의 파일 필수 입력
- SDF/MOL2 기본 지원
- SMILES/Meeko mapping 조건부 지원
- small molecule / peptide / protein partner 처리 분기
- 여러 pose 분리
- top n pose별 MD 작업 생성
- CIF receptor 입력 지원
- HETATM review required
- GROMACS 엔진 + ff14SB/GAFF2/AM1-BCC 고정 툴체인
- 50 ns default MD
- 100 ns 등 preset 선택
- queue 기반 작업 관리
- GPU 1개당 작업 1개 제어
- dashboard
- 작업 상태 표시
- 결과 다운로드

### 27.2 Should-have

- RMSD/RMSF/H-bond 등 자동 분석
- trajectory viewer
- MD movie 생성
- GPU enable/disable
- 작업 취소/재시작
- pose 간 비교
- storage dashboard

### 27.3 Could-have

- binding free energy 계산
- project 협업 기능
- email/slack notification
- SLURM 연동
- cloud deployment
- batch CSV submission
- OpenMM backend
- 수동 parameter 업로드

---

## 28. 주요 리스크 및 대응 방안

### 28.1 Ligand parameterization 실패

위험:

- ligand의 bond order, formal charge, protonation state가 불완전하거나 잘못 추정되면 parameterization이 실패하거나 잘못된 topology를 생성할 수 있다.

대응:

- SDF/MOL2를 기본 chemistry 정의 입력으로 요구한다.
- SMILES는 atom mapping 성공 시에만 허용한다.
- PDBQT 좌표에서 bond order를 perception하지 않는다.
- chemistry 정의와 pose 좌표의 원자 구성 불일치 시 작업 시작 전 검증 오류를 표시한다.
- antechamber/ACPYPE log를 제공한다.
- 수동 parameter 파일 업로드 기능을 Phase 4에 추가한다.

### 28.2 Peptide/protein ligand 처리 오류

위험:

- peptide ligand를 small molecule ligand처럼 GAFF2로 처리하면 부적절한 parameterization이 발생할 수 있다.

대응:

- ligand type을 small molecule, peptide, protein partner로 분기한다.
- peptide/protein ligand는 AMBER ff14SB 기반으로 처리한다.
- modified peptide는 custom parameter 필요 여부를 검증한다.

### 28.3 PDBQT pose parsing 문제

위험:

- docking tool 버전에 따라 PDBQT pose 구분 방식이 다를 수 있다.

대응:

- MODEL/ENDMDL 기반 parser
- score line parsing
- fallback parser 구현
- parsing 결과 미리보기 제공

### 28.4 HETATM 오분류

위험:

- CIF/PDB의 HETATM을 ligand로 자동 분류하면 cofactor, ion, water, additive가 잘못 처리될 수 있다.

대응:

- HETATM 기본값을 Review required로 설정한다.
- 사용자가 ligand/cofactor/ion/water/additive 분류를 확인한다.

### 28.5 저장공간 부족

위험:

- 50 ns, 100 ns MD trajectory 파일은 매우 클 수 있다.

대응:

- trajectory 저장 간격 제한
- compressed XTC 기본 사용
- 자동 압축
- 오래된 중간 파일 삭제 옵션
- Admin storage dashboard 제공

### 28.6 GPU 충돌

위험:

- 여러 worker가 동일 GPU를 사용할 가능성

대응:

- GPU lock table 사용
- worker별 CUDA_VISIBLE_DEVICES 고정
- 작업 시작 전 GPU lock 확인
- 작업 종료 또는 실패 시 lock 해제

### 28.7 서버 이전 어려움

위험:

- MD 도구와 GPU 환경 의존성으로 다른 서버 설치가 어려울 수 있다.

대응:

- Docker Compose 기반 배포
- `.env.example` 제공
- 설치 스크립트 제공
- NVIDIA Container Toolkit 설치 가이드 제공
- healthcheck 명령 제공

---

## 29. 완료 기준

본 플랫폼의 1차 완료 기준은 다음과 같다.

1. 사용자가 `http://서버IP:8888`로 접속할 수 있다.
2. `csbl / csbl` 초기 계정으로 로그인할 수 있다.
3. 최초 로그인 후 비밀번호 변경이 가능하다.
4. PDBQT 파일, ligand chemistry 정의 파일, receptor 구조를 함께 업로드할 수 있다.
5. 여러 pose를 자동 감지하고 상위 n개 pose를 선택할 수 있다.
6. SDF/MOL2 기반 chemistry 정의가 각 pose 좌표에 적용된다.
7. SMILES 또는 Meeko 기반 입력은 atom mapping 검증 성공 시에만 허용된다.
8. small molecule, peptide, protein partner 처리 경로가 구분된다.
9. pose와 chemistry 원자 구성 불일치 시 오류가 표시된다.
10. 각 pose별 MD 작업이 queue에 등록된다.
11. 기본 MD 조건은 50 ns로 설정된다.
12. 100 ns 등 preset을 선택할 수 있다.
13. 모든 작업은 GROMACS 엔진과 고정된 force field 툴체인으로 실행된다.
14. GPU 하나당 작업 하나가 실행된다.
15. Dashboard에서 작업 상태와 GPU 상태를 확인할 수 있다.
16. MD 완료 후 주요 분석 그래프를 확인할 수 있다.
17. MD trajectory 또는 영상 형태로 구조 변화를 확인할 수 있다.
18. 모든 결과 파일을 다운로드할 수 있다.
19. Docker Compose로 다른 서버에서도 실행 가능하다.

---

## 30. 권장 MVP 개발 일정

### Week 1–2: 설계 및 기본 인프라

- 요구사항 확정
- Docker Compose 설계
- Backend skeleton
- Frontend skeleton
- DB schema
- Login 구현
- 기본 dashboard layout

### Week 3–4: Job 및 Queue 구현

- 파일 업로드
- PDBQT parser
- SDF/MOL2 chemistry 검증
- SMILES/Meeko mapping 검증
- ligand type 분기
- CIF 변환 pipeline
- HETATM review UI
- job/sub-job 생성
- Redis queue
- worker 구조
- GPU lock 구현

### Week 5–6: MD Pipeline 구현

- structure preparation
- small molecule ligand parameterization
- peptide/protein ligand 처리 경로
- GROMACS MD template 구성
- 50 ns default workflow
- 10/50/100 ns preset
- GROMACS 실행 연동
- log capture

### Week 7–8: 결과 분석 및 시각화

- RMSD/RMSF/Rg/SASA/H-bond 분석
- Plotly graph
- trajectory viewer
- movie rendering
- result packaging
- zip download

### Week 9–10: 안정화 및 문서화

- 오류 처리 개선
- Admin 기능
- GPU control
- 저장공간 관리
- 설치 문서
- 테스트
- 연구실 서버 배포

---

## 31. 최종 권장 구현 방향

초기 버전은 지나치게 많은 MD 옵션을 제공하기보다, 연구실 표준 조건을 강제하는 방식이 바람직하다.

권장 MVP 원칙:

1. 기본 MD 조건은 50 ns로 고정한다.
2. 10 ns, 50 ns, 100 ns preset만 우선 제공한다.
3. MD 엔진은 GROMACS로 고정한다.
4. Force field 툴체인은 ff14SB / GAFF2 / AM1-BCC / ACPYPE로 고정한다.
5. PDBQT는 pose 좌표 소스로만 사용한다.
6. Ligand chemistry는 SDF/MOL2를 기본으로 하고, SMILES 또는 Meeko 기반 입력은 atom mapping 검증 성공 시에만 허용한다.
7. Small molecule은 GAFF2/AM1-BCC, peptide/protein ligand는 protein force field 기반으로 별도 처리한다.
8. HETATM은 자동 ligand 처리하지 않고 Review required로 둔다.
9. 작업 하나당 GPU 하나 원칙을 유지한다.
10. 모든 작업은 queue 기반으로 실행한다.
11. 결과는 pose별로 분리 저장한다.
12. Dashboard에서 작업, GPU, storage, 결과를 한 번에 확인한다.
13. Docker Compose만으로 설치 가능하게 구성한다.

이를 통해 연구실 구성원이 docking 결과를 업로드한 뒤 command-line 작업 없이 MD 시뮬레이션과 결과 해석까지 수행할 수 있는 실용적인 내부 플랫폼을 구축할 수 있다.
