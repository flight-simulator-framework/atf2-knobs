Random Excitation — Linac / BT / DR
===================================

LinacOptと同じPyQt6環境で動く、ランダム励起と変更命令の記録用GUIです。
BigDataLoggerと併用します。生のICT時系列、BPM波形、XSR画像の保存を重複させません。
GUIの表示はLinacOptに合わせて英語です。日本語フォントの追加は必要ありません。

起動
----
flight-simulator-framework/atf2-knobs ディレクトリで、LinacOptと同じPythonを使います。

  python Knobs/GUIs/RandomExcitation_GUI.py --demo

--demoは完全な模擬信号です。EPICSをimportせず、実機への接続や書き込みはありません。
実機用の起動は --demo を外します。

  python Knobs/GUIs/RandomExcitation_GUI.py

同じ起動方法: python -m Knobs.RandomExcitation [--demo]
パッケージを更新インストールした場合は RandomExcitation コマンドも使えます。
deploy_atf2.sh の既存GUI検出規則は、この新しい *_GUI.py も対象にします。
既に配備済みの環境では、このパッケージフォルダーとGUIランチャー、Knobs/timing_pv.pyをコピーすれば
上記のPython起動が可能です。sudoや新しいGUIライブラリは不要です。
依存: Python 3.11以降、PyQt6、NumPy、実機ではPyEPICSとlibca。
実機のEPICS_CA_ADDR_LIST等は、LinacOptで使用している環境設定を引き継ぎます。

操作
----
1. バックグラウンドのBigDataLoggerを起動しておきます。
2. Connect / read current limits を押します。これは購読と範囲確認だけで、PUTはしません。
3. 使用するグループ／個別制御を選び、表のLower/Upperを確認します。
   電流のLower/Upperは、確認したハードウェア範囲内で編集して狭められます。
   未接続、読み戻しなし、上下限不明などの制御は選択できません。
   6つのICTの初期値が有効になるまでSTARTは無効です。
4. Duration、Max. points、保存先を設定してSTARTを押します。
   開始時に設定値と上下限を再確認し、ログを作ってから動かします。
5. 上部の表示と色付きの区間、Active欄、表の色付き行で現在の操作対象を確認できます。
   Liveは読み戻し、Targetは要求した論理設定です。操作するPV名はControl列のtooltipにあります。
   Phaseが360度の境界をまたぐ場合、TargetとPVに送る正規化角度は360度だけ異なることがあります。

選べる制御
----------
・RF位相10個: GUN、L0〜L8。1度刻み、開始値±90度が拡大上限の初期値。
・SOLENOIDE、QA1L〜QA5L、QM1L〜QM3L。0.1 A刻み。
・Linacステアリング24個、BTステアリング22個。0.1 A刻み。
・Timing: Coarse/Fineのいずれか。EVE_LINAC:OUT0:SetDataをns単位で扱います。
・DRステアリング97個は任意に追加できます。初期選択はOFFです。
  Linac/BTの応答を調べる初回運転ではDRを固定し、DR自体の応答を調べるときにONにします。
  Skew電源やDR主四極は、このLinacOpt対象の励起には含めていません。

初期選択はLinac/BTの66論理制御です。全制御を毎回同時に動かすわけではありません。
各群を順に巡回し、群内の候補をランダムな順序で均等に取り上げます。
Max. controls / pointの初期値は10で、個別に変更できます。
Joint every Nの初期値4では群間の同時変更、Reference every Nの初期値10では
開始時の設定を再測定します。0を指定すると、その種類の挿入を無効にします。
その時点の探索範囲の離散格子から一様に設定を選び、中心点は開始時の設定に固定します。
seedと全命令を記録します。参照・初期測定・再開時の再測定も測定点数に含みます。

段階的な範囲拡大（2026-10-28）
------------------------------
探索は開始値の±1格子から始めます。初期設定では位相±1度、電流±0.1 A、
Timing Coarseなら±11.2 ns、Fineなら±2.8 nsです。
GUIのLower/Upperは最終的な拡大上限です。電流の装置上下限全体を
実機探索の許容範囲とみなさず、実際に探索する範囲に狭めてください。
開始値がLower/Upperの外にある場合は開始できません。

同じ段階で次の全条件を満たした後、初期設定に戻して新しい測定を行い、
その測定も条件を満たせば半幅を2倍（±1→±2→±4格子…）にします。
・設定が実際に変わった探索点が40点以上、必要なICT標本がそろっている。
・選択した全制御について、それぞれ2回以上の変更後測定がある。
・DR/LN0が最初の有効な測定値の10％以上である。
参照点、同じ値の再測定、途中の移動は、拡大用の変更回数には数えません。
40点/2回はGUIで変更できます。拡大上限で打ち止めになり、時間だけでは広がりません。

10％は「開始時の比の0.1倍」です。開始時DR/LN0=0.8なら下限0.08です。
「開始時から10％低下したら停止」ではありません。基準値は運転中に下げません。
監視には最新monitor値、測定後判定にはICT平均値の比を使います。
同一パルスの透過率や放射線量の保証ではなく、探索の品質判定です。
開始時にLN0/DRが正の有効値でない、または標本不足なら探索を開始しません。

各命令の変更幅も初期値1格子に制限します。離れた点への移動、基準点への
復帰、終了時復帰も分割し、各段階で読み戻し・新しいLN0/DR通知・安定時間を
確認します。途中値は元の格子に乗らない場合があります（小数の開始値を保持）。
このため1点の所要時間は距離に依存し、従来の一括変更より長くなることがあります。
途中の命令は *_transition として記録し、完了した定常測定とは分けます。

下限割れ、LN0/DRのゼロ・負値・無効値・切断・通知途絶（初期値2秒）、
必要な測定標本の不足ではQUALITY HOLDに入ります。命令を中断し、
半幅を1段階縮め（最小は初期半幅）、そのrun中はそれ以上の再拡大を行いません。
範囲の縮小は次の探索候補に対する制限であり、磁石を自動で戻す操作ではありません。
×/Esc/Stay on holdは停止保持です。通常のPAUSEの×/Escとは異なります。
施設の手順でビーム状態を確認・回復した後、Recheck after recoveryを選びます。
ICTが回復していなければ命令を送らず再びHOLDになります。回復していれば
初期設定へ変更幅を制限して戻し、再測定してから狭い範囲の探索を再開します。
Endは現在の設定を保持して終了します。異常時に最良点へ自動移動しません。

このGUIはビーム停止命令を送りません。QUALITY HOLDやSTOPでもビーム自体は
止まりません。放射線モニター・ロスモニターによる施設保護系の代替ではありません。
10％は放射線安全の許容値ではなく、施設で認められたstudy条件内で使用する設定です。

電流範囲と刻み
--------------
op-base/magnet/Qt/Library/MagnetLibrary/magnet.cpp の定義に従い、
  <磁石名>:parameter.A = 下限
  <磁石名>:parameter.B = 上限
を確認します。上下が逆なら入れ替えて推測せず、その制御を無効にします。
これらを読めない場合は、有効なEPICS CTRL上下限を使い、範囲の根拠を表に表示します。
双方に有効な上下限があれば共通範囲に制限します。どちらも不明なら操作できません。
表の初期Lower/Upper（拡大上限）はこの上下限全体です。通常のビーム調整範囲を自動推定するものではありません。
運転点の近くを調べる場合は表でLower/Upperを狭めます。
GUIの位相幅やTimingを変更しても、編集済みの電流範囲は保持します。
実行中もparameter.A/Bのmonitor値を参照し、送信直前に範囲を再確認します。

parameter.A/Bは設定保存用フィールドのため、接続済みで有限の数値を持ち、
アラームがUDF/INVALID（status=17、severity=3）の場合に限り値を採用します。
未処理のcalcレコードでA=-5、B=+5がUDFになるケースへの対応です。
表には通常どおり「parameter.A / parameter.B」と表示し、INVALIDの注記は出しません。
CTRL上下限があれば共通範囲を使い、A>=Bなら採用しません。
切断・NaN・無限大・他のINVALIDアラームは、この例外では採用しません。
磁石のcurrentWriteも設定保存用PVとして、INVALIDでも接続済みで有限の単一数値なら
正常な設定値として採用し、StatusにINVALIDを表示しません。切断・非数値・NaN・無限大は
採用しません。上下限と書き込み権限の確認は維持します。
ICT、currentReadなどの読み戻し、位相・タイミングPVのアラーム判定は従来どおりです。

ランダムな位相設定は整数度、電流設定は0.1 Aの整数倍です。
開始値が格子に乗っていなくても、参照測定・終了時の復帰では開始値をそのまま戻します。
この例外はexact_restoreとして命令ログに明示します。
360度全域を扱うCTRL範囲が分かる位相は境界を折り返し、位相差も周期を考慮します。
より狭いCTRL範囲がある位相は、±幅との共通範囲を使用します。

TimingはLinacOptと同じ定義です。
  使用PV: EVE_LINAC:OUT0:SetData [ns]。
  Coarse: 最大範囲は開始値±112 ns、11.2 ns刻み。
  Fine: 最大範囲は開始値±11.2 ns、2.8 ns刻み。
開始時の細かい桁を保持した相対格子です。OT8D/RFDLY0は購読・操作しません。
命令ログにはSetDataへのns値を記録します。

測定と待機
----------
変更命令を送り、選択された制御の読み戻しが目標の許容内になるのを待ちます。
到達した後は初期値1秒の確認区間を置き、逸脱したら確認区間をやり直します。
初期許容差はPhase 0.5度、Current 0.05 Aで、Detailsで変更できます。
到達後、新しいICT通知を各PV6個取得します。固定5秒の空待ちはありません。
初期timeoutは到達15秒、測定10秒。Detailsから変更できます。

参照ICTは InterfaceATF2_Linac.read_icts_for_optimizer() と同じ実PVです。
  BIM:GUN:nparticles、BIM:LN0:nparticles、BIM:LNE:nparticles、
  BIM:BTM:nparticles、BIM:BTE:nparticles、BIM:DR:nparticles。
仮値を返す汎用get_icts()は使用しません。負値やゼロを自動的にクリップしません。
接続状態・アラームを確認し、測定窓開始後に受けた、IOC時刻が進んだ通知を数えます。
同じキャッシュを6回読んで6標本とは数えません。新規通知が不足すれば不完全な測定として記録します。
一定値でIOCが通知を出さない場合も標本不足になります。一定値というだけでゼロビームを捏造しません。
PVごとの観測窓で平均するため、画面のDR/LN0は参考比です。同一パルスを保証する輸送効率ではありません。
調整対象の設定が測定途中に変わった場合、混ざった測定を最良値に使わずエラー停止します。

独立した読み戻しが定義されていないGUN/L0は、設定値echoと確認時間による到達判定です。
SOLENOIDEは既存LinacOptと同じinternalCurrentWriteを参照します。
これらを実位相・実磁場の到達確認とは扱わず、feedbackの種類を記録します。
個々の装置の物理的な整定時間は、実機で確認する必要があります。

接続は継続したCA monitor（camonitorと同じ購読方式）です。
表示タイマーや測定待ちループはローカルキャッシュを読むだけで、繰り返しGETを送りません。
Connect時の範囲メタデータ取得は別です。書き込みにはCA put完了確認を使い、
さらに読み戻しを確認します。CA put完了だけを物理的な整定とは扱いません。

DR / XSR
--------
DR:monitorsは受信と画面表示だけです。DRBPM:ORBIT_MODEとDRBPM:INJ:TURN_COUNTも読み取ります。
このアプリからモードやターン番号を変えることはできません。
DRの波形は10要素/スロットとして読み、status、x、y、intensityを解釈します。
有効な96 BPMの軌道を既存interfaceと同じ換算でmm表示します。
XSRのV/H SIGMA_AVEとMEAN_AVEを購読して表示します。既存BigDataと重複して保存しません。
通知の古さは表示しますが、値変化通知がないだけで装置故障とは断定しません。

Pause / Resume / 終了
---------------------
PAUSE: 現在の設定変更・到達確認・測定を終えてから停止し、設定と履歴を保持します。
       即時停止とは異なり、timeoutを待つこともあります。
       LinacOptと同じく、停止した時点で選択ダイアログを表示します。
Resume: 同じrunで、停止した点の選択制御を再適用・再測定してから次の点へ進みます。
        Pause中に人が設定を変えた場合も、再開時には停止した点の設定を再適用します。
Save and End: 最良の有効測定時の全選択制御を適用して終了します。追加測定はしません。
              最良値がない場合、この選択肢は無効です。
Esc／ダイアログを閉じる: LinacOptと同じくResumeです。
STOP: 以後の命令を止めて終了します。送信済みの命令は取り消さず、設定は保持します。
      アプリを閉じた場合もこの停止処理を待ちます。

最良の判定はBest-point ICTの平均値で、初期選択はDRです。
全ICTに必要数の新しい標本があり、LN0と選んだICTが正のときだけ最良候補になります。
これは単一の観測スコアです。XSRやDR軌道まで含めた最適性を保証するものではありません。
Duration/Max. pointsによる通常終了時は、On completionに従います。
初期値は開始時設定への復帰です。best／現状保持も選べます。
DurationにはPause時間を含み、現在の測定区間を完了してから時間上限を判定します。

保存内容
--------
実機用の初期保存先:
  /mnt/nas1/atf-data/op/Bigdata/commands/YYYY/YYYYMMDD_HHMMSS_<run-id>/
同じ秒でもrun-idが異なるため上書きしません。保存先はGUIで指定できます。
デモの初期保存先はカレントディレクトリのAnalysis/RandomExcitationDemoです。

commands.jsonl（追記形式）:
  session_start: 設定、PV一覧、原点、探索範囲、feedback種類、ログスキーマ、乱数seed。
  step_request: step_id、random_index、変更対象と要求値。
  command_intent: 送信する前にflush/fsync。PV名、変更前値、指令値、命令ID、時刻。
  command_ack / command_error: CA put完了または例外。エラー時の適用成否はunknownとする。
  arrival_confirmed: 到達確認区間完了とfeedbackの種類。
  measurement_started / measurement_window: 測定開始・終了、各ICTの標本数とIOC時刻範囲、品質。
  best_observed: 最良スコアと、そのときの選択制御の全設定。
  paused / resumed / session_end: 休止・再開・終了と最終状態。
  exploration_range: 各段階の実際の範囲、基準比、下限、拡大・縮小の理由。
  quality_evaluation: 各完了測定のDR/LN0と固定基準・下限。
  quality_hold / quality_recheck_requested / quality_stop: 異常時の値、保持・再確認・終了。

ログスキーマはrandom-excitation-commands/2です。途中で中断した測定は
measurement_windowがなくてもquality_holdに最新ICT値と時刻を残します。

各イベントにUTCナノ秒時刻とISO8601時刻、run_idがあります。
同じstep_idの命令と測定窓を、BigDataの時刻に対応づけて解析できます。
停止・失敗でmeasurement_windowがないstepは、完了した定常測定とは扱いません。
command_intentだけでackがない命令は、通信断・プロセス終了などで適用成否不明です。
プログラムはログの記録に失敗すると、その先の命令を送りません。

steps.csv:
  完了した測定窓のstep_id、種類、開始・終了UTC時刻、標本数、品質、スコア、変更対象。
  Save ICT point means tooを選んだ場合だけ、6 ICTの平均を追加します（初期値OFF）。
  最良設定を選ぶためのスコアは、最小限の評価記録として常に保存します。
生波形、パルスごとの全測定値、画像を重複保存しません。
BigDataの保存をこのアプリで自動開始・停止する機能はありません。

背景ロガーの保存範囲
------------------
GUIでICTやDR軌道が見えていても、その生データが背景のBigDataに保存されている証拠にはなりません。
BigDataLogger 0.8では、BIM:*:nparticlesの6 PV、LINAC:monitors、DR:monitors、
DRBPMのモード／ターン、位相・タイミング・磁石設定値を、.binと同名の.pvsへ保存します。
実行環境のロガーを0.8へ更新・再ビルドし、.binと.pvsをセットで保管してください。
0.6以前のロガーにはこれらの購読がありません。--no-slowは追加取得も無効にします。
read_pv_archive.pyで各PVの実際の取得件数と状態を確認できます。
このGUIのcommands.jsonlとsteps.csvは操作記録であり、未取得のビーム波形を補完しません。
Save ICT point means tooで6 ICTの測定窓平均を残すことはできますが、
同一パルスでのtransmissionやDR軌道の後解析には元の測定値が必要です。

検証
----
ローカルの基本テスト（実機アクセスなし）:
  QT_QPA_PLATFORM=offscreen python -m unittest discover -s Knobs/RandomExcitation/tests -v

任意の実EPICS通信テストにはcaprotoを使用します。
  python -m Knobs.RandomExcitation.tests.check_local_ioc
TEST専用PV名、127.0.0.1、独立したポートを設定する別プロセス用のテストです。
実際のPyEPICSとGUIのQThread経由で接続・収集・書き込み・復帰を検証します。
このテストでも、実加速器への接続や運転はしません。

2026-10-28検証: 43件のユニット/GUI試験に合格。10％の意味、固定基準、
全制御の変更実績と参照再測定を伴う拡大、上限、縮小、欠測/切断/低下での
保持、異常中の再開拒否、GUIの×/Escでの保持を確認しました。
ローカルIOCでは実PyEPICSで12点・20命令、段階的拡大、1格子以内の変更と
初期値復帰を確認。実際のQt workerでの3点測定も合格しました。
放射線・実機の整定時間・現場インターロックとの連携を検証したものではありません。
