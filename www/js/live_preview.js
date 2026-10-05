$(document).ready(function() {

	var dialog;
	var modalOpened = false;
	var ledsim_width = 550;
	var ledsim_height = 489;

	var canvas_height;
	var canvas_width;
	var imageCanvasNodeCtx;
	var ledsCanvasNodeCtx;

	var leds;
	var lC = false;
	var twoDPaths = [];
	var lastFeedFrame = Date.now();
	var toggleLedsNum = false;

	CanvasRenderingContext2D.prototype.clear = function(){
		this.clearRect(0, 0, this.canvas.width, this.canvas.height)
	};

	function create2dPaths(){
		twoDPaths = [];
		for(var idx=0; idx<leds.length; idx++)
		{
			var led = leds[idx];
			twoDPaths.push( build2DPath(led.hmin * canvas_width, led.vmin * canvas_height, (led.hmax-led.hmin) * canvas_width, (led.vmax-led.vmin) * canvas_height, 5) );
		}
	};
	
	function build2DPath(x, y, width, height, radius) {

		if (typeof radius == 'number')
		{
			radius = {tl: radius, tr: radius, br: radius, bl: radius};
		}
		else
		{
			var defaultRadius = {tl: 0, tr: 0, br: 0, bl: 0};
			for (var side in defaultRadius)
			{
				radius[side] = radius[side] || defaultRadius[side];
			}
		}

		var path = new Path2D();

		path.moveTo(x + radius.tl, y);
		path.lineTo(x + width - radius.tr, y);
		path.quadraticCurveTo(x + width, y, x + width, y + radius.tr);
		path.lineTo(x + width, y + height - radius.br);
		path.quadraticCurveTo(x + width, y + height, x + width - radius.br, y + height);
		path.lineTo(x + radius.bl, y + height);
		path.quadraticCurveTo(x, y + height, x, y + height - radius.bl);
		path.lineTo(x, y + radius.tl);
		path.quadraticCurveTo(x, y, x + radius.tl, y);

		return path;
	};

	function takeCareButton(force = false)
	{
		if($("#live_preview_dialog").outerWidth() < 550 || force)
		{
			$('#vid_btn_1').text($.i18n('main_ledsim_btn_toggleleds').substring(0,4));
			$('#vid_btn_2').text($.i18n('main_ledsim_btn_togglelednumber').substring(0,4));
			$('#vid_btn_3').text($.i18n('main_ledsim_btn_togglelivevideo').substring(0,4));
			$('#vid_btn_5').text($.i18n('main_ledsim_btn_cropvideo').substring(0,4));
			$('#vid_btn_4').text($.i18n('main_ledsim_btn_screenshot').substring(0,4));
		}
		else
		{			
			$('#vid_btn_1').text($.i18n('main_ledsim_btn_toggleleds'));
			$('#vid_btn_2').text($.i18n('main_ledsim_btn_togglelednumber'));
			$('#vid_btn_3').text($.i18n('main_ledsim_btn_togglelivevideo'));
			$('#vid_btn_5').text($.i18n('main_ledsim_btn_cropvideo'));
			$('#vid_btn_4').text($.i18n('main_ledsim_btn_screenshot'));
		}
	};

	function getReady()
	{
		leds = window.serverConfig.leds;

		if(getStorage('ledsim_width') != null)
		{
			ledsim_width = getStorage('ledsim_width');
			ledsim_height = getStorage('ledsim_height');
		}

		dialog = $("#live_preview_dialog").dialog({
			uiLibrary: 'bootstrap5',
			resizable: true,
			modal: false,
			minWidth: 330,
			width: ledsim_width,
			minHeight: 320,
			height: ledsim_height,
			closeOnEscape: true,
			autoOpen: false,
			title: '<svg data-src="svg/live_preview.svg" fill="currentColor" class="svg4hyperhdr"></svg>' + $.i18n('main_ledsim_title'),
			resize: function (e) {
				updateLedLayout();
			},
			opened: function (e) {
				if(!lC)
				{
					updateLedLayout();
					lC = true;
				}
				modalOpened = true;

				if($('#leds_toggle').hasClass('btn-success'))
					requestLedColorsStart();

				if($('#leds_toggle_live_video').hasClass('btn-success'))
					requestLedImageStart();
			},
			closed: function (e) {
				modalOpened = false;
				requestLedColorsStop();
				if (!window.ledPreviewImageActive)
					requestLedImageStop();
				if (liveCropActive)
					toggleLiveVideoCrop();
			},
			resizeStop: function (e) {
				setStorage("ledsim_width", $("#live_preview_dialog").outerWidth());
				setStorage("ledsim_height", $("#live_preview_dialog").outerHeight());
				
				takeCareButton();
				updateLedLayout();
			}
		});

		var ua = window.navigator.userAgent;
		var iOS = !!ua.match(/iPad/i) || !!ua.match(/iPhone/i);
		var webkitDefined = !!ua.match(/WebKit/i);
		var iOSSafari = iOS && webkitDefined && !ua.match(/CriOS/i);

		if (navigator.maxTouchPoints > 0 || iOSSafari)
		{
			var btnn = $("#live_preview_dialog").find("button.btn-close");
			if (btnn.length == 1 && !$(btnn[0]).parent().is($("#live_preview_dialog")))
			{
				$(btnn[0]).css('position','absolute');
				$(btnn[0]).css('right','10px');
				$(btnn[0]).css('top','20px');
				$("#live_preview_dialog").append( $(btnn[0]) );
			}
		}
		
		$(window.hyperhdr).on("cmd-config-getconfig",function(event){
			leds = event.response.info.leds;
			updateLedLayout();
		});

		$(window.hyperhdr).on("cmd-instance-switchTo", function (event) {
			setTimeout(function(){
				if($('#live_preview_dialog').is(':visible'))
				{
					if ( $('#leds_toggle_live_video').hasClass("btn-success"))
					{
						requestLedImageStart();					
					}
					if ( $('#leds_toggle').hasClass("btn-success"))
					{
						requestLedColorsStart();					
					}
				}
			}, 500);
		});
	};

	function resetImage(){
		imageCanvasNodeCtx.fillStyle = "rgb(0,0,0)"
		imageCanvasNodeCtx.fillRect(0, 0, canvas_width, canvas_height);

		var image = new Image();
		var sourceImg = document.getElementById("left_top_hyperhdr_logo");
		if (sourceImg.naturalWidth > 0 && sourceImg.naturalHeight > 0)
		{
			var x = Math.max(canvas_width/2 - 130, 0);
			var y = Math.max(canvas_height/2 - 38, 0);
			imageCanvasNodeCtx.drawImage(sourceImg, x, y, 262, 83);

			imageCanvasNodeCtx.font = "16px monospace";
			imageCanvasNodeCtx.textAlign = "center";
			imageCanvasNodeCtx.fillStyle = "white";

			if (!window.imageStreamActive)
			{
				imageCanvasNodeCtx.fillText($.i18n('preview_live_video_is_paused'), x + 131, y + 100);
			}
			else
			{
				imageCanvasNodeCtx.fillText($.i18n('preview_no_signal'), x + 131, y + 100);
			}
		}
	};	
	
	$(window.hyperhdr).one("ready",function(){
		getReady();		
	});

	function printLedsToCanvas(colors)
	{
		if(!window.ledStreamActive)
			return;

		var useColor = false;
		var cPos = 0;
		ledsCanvasNodeCtx.clear();

		if(typeof colors != "undefined")
			useColor = true;

		if(colors && colors.length/3 < leds.length)
			return;

		for(var idx=0; idx<leds.length; idx++)
		{
			var led = leds[idx];

			ledsCanvasNodeCtx.fillStyle = (useColor) ?  "rgba("+colors[cPos]+","+colors[cPos+1]+","+colors[cPos+2]+",0.75)"  : "hsla("+(idx*360/leds.length)+",100%,50%,0.75)";
			ledsCanvasNodeCtx.fill(twoDPaths[idx]);
			ledsCanvasNodeCtx.stroke(twoDPaths[idx]);

			if(toggleLedsNum)
			{
				ledsCanvasNodeCtx.fillStyle = "white";
				ledsCanvasNodeCtx.textAlign = "center";
				ledsCanvasNodeCtx.fillText(((led.name) ? led.name : idx), (led.hmin * canvas_width) + ( ((led.hmax-led.hmin) * canvas_width) / 2), 3 + (led.vmin * canvas_height) + ( ((led.vmax-led.vmin) * canvas_height) / 2));
			}

			cPos += 3;
		}
	};

	function leftPad(num, size) {
		num = num.toString();
		while (num.length < size) num = "0" + num;
		return num;
	};

	$('#leds_screenshot').off().on("click", function() {
		var link = document.createElement('a');
		var d = new Date();
		link.download = 'shot' + leftPad(d.getHours(),2) + leftPad(d.getMinutes(),2) + leftPad(d.getSeconds(),2) + '.png';
		link.href = document.getElementById("image_preview_canv").toDataURL()
		link.click();
		link.remove();
	});

	$('#leds_toggle').off().on("click", function() {		
		ledsCanvasNodeCtx.clear();
		setClassByBool('#leds_toggle', window.ledStreamActive, "btn-danger", "btn-success");
		if ( window.ledStreamActive )
		{
			requestLedColorsStop();
		}
		else
		{
			requestLedColorsStart();
		}
	});

	$('#leds_toggle_num').off().on("click", function() {
		toggleLedsNum = !toggleLedsNum;
		toggleClass('#leds_toggle_num', "btn-danger", "btn-success");
	});
	
	$('#leds_toggle_live_video').off().on("click", function() {
		setClassByBool('#leds_toggle_live_video', window.imageStreamActive, "btn-danger", "btn-success");
		if ( window.imageStreamActive )
		{
			lastFeedFrame = Date.now();
			requestLedImageStop();
			resetImage();
		}
		else
		{
			lastFeedFrame = Date.now();			
			requestLedImageStart();
			resetImage();
			feedWatcher();			
		}
	});

	$('#vid_btn_crop').off().on("click", function() {
		toggleLiveVideoCrop();
	});

	$("#btn_open_ledsim").off().on("click", function(event) {
		
		if (dialog == null ||typeof dialog === 'undefined')
			getReady();
		
		if (window.innerWidth < 740)
		{
			$("#live_preview_dialog").width(330);
			$("#live_preview_dialog").height(320);
			
			$("#live_preview_dialog").css('top', '10px');
			$("#live_preview_dialog").css('left', '20px');			
		}
		else
		{
			var t = parseInt($('#live_preview_dialog').css('top'), 10);
			var l = parseFloat($('#live_preview_dialog').css('left'), 20);
			if ( !isNaN(t) && (t < 10 || t + 25 > window.innerHeight))
				$("#live_preview_dialog").css('top', '10px');
			if ( !isNaN(l) && (l < 20 || l + 50 > window.innerWidth))
				$("#live_preview_dialog").css('left', '20px');
		}
		$("#live_preview_dialog").css('position', 'fixed');
		
		takeCareButton();
		
		dialog.open();
		
		updateLedLayout();
	});

	var liveCropActive = false;
	var curCropL = 0, curCropT = 0, curCropR = 0, curCropB = 0;
	var backupCrop = null;
	var isDraggingLiveCrop = false;

	function toggleLiveVideoCrop()
	{
		liveCropActive = !liveCropActive;
		if (liveCropActive)
		{
			$('#vid_btn_crop').removeClass('btn-warning').addClass('btn-success');
			if (!window.imageStreamActive)
			{
				$('#leds_toggle_live_video').trigger('click');
			}

			// Ensure minimum dimensions for comfortable cropping
			if ($("#live_preview_dialog").outerWidth() < 600 || $("#live_preview_dialog").outerHeight() < 500)
			{
				$("#live_preview_dialog").width(Math.max($("#live_preview_dialog").outerWidth(), 640));
				$("#live_preview_dialog").height(Math.max($("#live_preview_dialog").outerHeight(), 520));
			}

			$('#live_crop_toolbar').removeClass('d-none');

			// Read current crop from config
			if (window.serverConfig && window.serverConfig.videoGrabber)
			{
				curCropL = window.serverConfig.videoGrabber.cropLeft || 0;
				curCropR = window.serverConfig.videoGrabber.cropRight || 0;
				curCropT = window.serverConfig.videoGrabber.cropTop || 0;
				curCropB = window.serverConfig.videoGrabber.cropBottom || 0;
			}
			else
			{
				curCropL = curCropR = curCropT = curCropB = 0;
			}

			backupCrop = { l: curCropL, t: curCropT, r: curCropR, b: curCropB };

			// Temporarily send crop 0 to engine so user sees the uncropped live feed with handles at current crop
			if (curCropL > 0 || curCropR > 0 || curCropT > 0 || curCropB > 0)
			{
				sendToHyperhdr("video-crop", "", '"crop":{"left":0,"right":0,"top":0,"bottom":0}');
			}

			updateLedLayout();
			renderLiveCropOverlay();
		}
		else
		{
			$('#vid_btn_crop').removeClass('btn-success').addClass('btn-warning');
			$('#live_crop_toolbar').addClass('d-none');
			$('#live_video_crop_overlay').remove();
			updateLedLayout();
		}
	}

	function renderLiveCropOverlay()
	{
		if (!liveCropActive)
		{
			$('#live_video_crop_overlay').remove();
			return;
		}

		var res = getNativeGrabberResolution();
		var nativeW = res.width;
		var nativeH = res.height;

		var cw = canvas_width;
		var ch = canvas_height;

		var x1 = (curCropL / nativeW) * cw;
		var y1 = (curCropT / nativeH) * ch;
		var x2 = ((nativeW - curCropR) / nativeW) * cw;
		var y2 = ((nativeH - curCropB) / nativeH) * ch;

		var rectW = Math.max(0, x2 - x1);
		var rectH = Math.max(0, y2 - y1);

		var midX = (x1 + x2) / 2;
		var midY = (y1 + y2) / 2;

		$('#live_crop_txt_l').text(curCropL);
		$('#live_crop_txt_t').text(curCropT);
		$('#live_crop_txt_r').text(curCropR);
		$('#live_crop_txt_b').text(curCropB);
		$('#live_crop_txt_size').text((nativeW - curCropL - curCropR) + 'x' + (nativeH - curCropT - curCropB) + ' / ' + nativeW + 'x' + nativeH);

		var maskPath = `M 0 0 L ${cw} 0 L ${cw} ${ch} L 0 ${ch} Z M ${x1} ${y1} L ${x1} ${y2} L ${x2} ${y2} L ${x2} ${y1} Z`;

		if ($('#live_video_crop_overlay').length === 0)
		{
			var overlayHtml = `
			<div id="live_video_crop_overlay" class="hyperhdr-crop-overlay" style="z-index: 100000;">
				<svg id="live_crop_svg" class="hyperhdr-crop-svg" width="${cw}" height="${ch}">
					<path id="live_crop_mask" d="${maskPath}" fill="rgba(0, 0, 0, 0.52)" fill-rule="evenodd" />
					<rect id="live_crop_rect" x="${x1}" y="${y1}" width="${rectW}" height="${rectH}" fill="rgba(0, 210, 255, 0.05)" stroke="#00d2ff" stroke-width="2.5" stroke-dasharray="6,4" />
				</svg>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-corner" id="live_handle_tl" style="left:${x1}px; top:${y1}px;" data-handle="tl" title="Top-Left Corner">TL</div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-corner" id="live_handle_tr" style="left:${x2}px; top:${y1}px;" data-handle="tr" title="Top-Right Corner">TR</div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-corner" id="live_handle_br" style="left:${x2}px; top:${y2}px;" data-handle="br" title="Bottom-Right Corner">BR</div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-corner" id="live_handle_bl" style="left:${x1}px; top:${y2}px;" data-handle="bl" title="Bottom-Left Corner">BL</div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-edge-h" id="live_handle_et" style="left:${midX}px; top:${y1}px;" data-handle="edge-t" title="Drag top edge"></div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-edge-v" id="live_handle_er" style="left:${x2}px; top:${midY}px;" data-handle="edge-r" title="Drag right edge"></div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-edge-h" id="live_handle_eb" style="left:${midX}px; top:${y2}px;" data-handle="edge-b" title="Drag bottom edge"></div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-edge-v" id="live_handle_el" style="left:${x1}px; top:${midY}px;" data-handle="edge-l" title="Drag left edge"></div>
				<div class="hyperhdr-crop-handle hyperhdr-crop-handle-center" id="live_handle_center" style="left:${midX}px; top:${midY}px;" data-handle="center" title="Pan crop box">
					<svg data-src="svg/button_move.svg" style="width:14px;height:14px;" fill="currentColor" class="svg4hyperhdr"></svg>
				</div>
				<div id="live_crop_tooltip" class="hyperhdr-crop-tooltip d-none"></div>
			</div>`;
			$('#leds_canvas').append(overlayHtml);
			bindLiveCropOverlayEvents();
		}
		else
		{
			$('#live_crop_svg').attr({ width: cw, height: ch });
			$('#live_crop_mask').attr('d', maskPath);
			$('#live_crop_rect').attr({ x: x1, y: y1, width: rectW, height: rectH });
			$('#live_handle_tl').css({ left: x1 + 'px', top: y1 + 'px' });
			$('#live_handle_tr').css({ left: x2 + 'px', top: y1 + 'px' });
			$('#live_handle_br').css({ left: x2 + 'px', top: y2 + 'px' });
			$('#live_handle_bl').css({ left: x1 + 'px', top: y2 + 'px' });
			$('#live_handle_et').css({ left: midX + 'px', top: y1 + 'px' });
			$('#live_handle_er').css({ left: x2 + 'px', top: midY + 'px' });
			$('#live_handle_eb').css({ left: midX + 'px', top: y2 + 'px' });
			$('#live_handle_el').css({ left: x1 + 'px', top: midY + 'px' });
			$('#live_handle_center').css({ left: midX + 'px', top: midY + 'px' });
		}
	}

	function bindLiveCropOverlayEvents()
	{
		var activeHandle = null;
		var startPointer = { x: 0, y: 0 };
		var startCrop = {};

		$('#live_video_crop_overlay .hyperhdr-crop-handle').off('pointerdown').on('pointerdown', function(e) {
			e.preventDefault();
			e.stopPropagation();

			activeHandle = $(this).data('handle');
			startPointer = { x: e.clientX, y: e.clientY };
			startCrop = { l: curCropL, t: curCropT, r: curCropR, b: curCropB };

			isDraggingLiveCrop = true;
			$(this).addClass('active');

			if (this.setPointerCapture) {
				this.setPointerCapture(e.pointerId);
			}

			$('#live_crop_tooltip').removeClass('d-none');
		});

		$('#live_video_crop_overlay .hyperhdr-crop-handle').off('pointermove').on('pointermove', function(e) {
			if (!isDraggingLiveCrop || !activeHandle) return;

			e.preventDefault();
			e.stopPropagation();

			var res = getNativeGrabberResolution();
			var nativeW = res.width;
			var nativeH = res.height;

			var scaleX = nativeW / canvas_width;
			var scaleY = nativeH / canvas_height;

			var deltaPxX = (e.clientX - startPointer.x) * scaleX;
			var deltaPxY = (e.clientY - startPointer.y) * scaleY;

			var step2 = function(val, maxVal) {
				var v = Math.round(val / 2) * 2;
				return Math.max(0, Math.min(maxVal - 4, v));
			};

			var tooltipText = "";

			if (activeHandle === 'tl')
			{
				curCropL = step2(startCrop.l + deltaPxX, nativeW - startCrop.r);
				curCropT = step2(startCrop.t + deltaPxY, nativeH - startCrop.b);
				tooltipText = `Left: ${curCropL}px, Top: ${curCropT}px`;
			}
			else if (activeHandle === 'tr')
			{
				curCropR = step2(startCrop.r - deltaPxX, nativeW - startCrop.l);
				curCropT = step2(startCrop.t + deltaPxY, nativeH - startCrop.b);
				tooltipText = `Right: ${curCropR}px, Top: ${curCropT}px`;
			}
			else if (activeHandle === 'br')
			{
				curCropR = step2(startCrop.r - deltaPxX, nativeW - startCrop.l);
				curCropB = step2(startCrop.b - deltaPxY, nativeH - startCrop.t);
				tooltipText = `Right: ${curCropR}px, Bottom: ${curCropB}px`;
			}
			else if (activeHandle === 'bl')
			{
				curCropL = step2(startCrop.l + deltaPxX, nativeW - startCrop.r);
				curCropB = step2(startCrop.b - deltaPxY, nativeH - startCrop.t);
				tooltipText = `Left: ${curCropL}px, Bottom: ${curCropB}px`;
			}
			else if (activeHandle === 'edge-t')
			{
				curCropT = step2(startCrop.t + deltaPxY, nativeH - startCrop.b);
				tooltipText = `Top: ${curCropT}px`;
			}
			else if (activeHandle === 'edge-b')
			{
				curCropB = step2(startCrop.b - deltaPxY, nativeH - startCrop.t);
				tooltipText = `Bottom: ${curCropB}px`;
			}
			else if (activeHandle === 'edge-l')
			{
				curCropL = step2(startCrop.l + deltaPxX, nativeW - startCrop.r);
				tooltipText = `Left: ${curCropL}px`;
			}
			else if (activeHandle === 'edge-r')
			{
				curCropR = step2(startCrop.r - deltaPxX, nativeW - startCrop.l);
				tooltipText = `Right: ${curCropR}px`;
			}
			else if (activeHandle === 'center')
			{
				var cropW = nativeW - startCrop.l - startCrop.r;
				var cropH = nativeH - startCrop.t - startCrop.b;

				var newL = step2(startCrop.l + deltaPxX, nativeW - cropW);
				var newT = step2(startCrop.t + deltaPxY, nativeH - cropH);

				curCropL = newL;
				curCropR = nativeW - cropW - newL;
				curCropT = newT;
				curCropB = nativeH - cropH - newT;

				tooltipText = `Pan: L:${curCropL}px, T:${curCropT}px`;
			}

			var rectCanvas = document.getElementById("leds_canvas").getBoundingClientRect();
			var tipX = e.clientX - rectCanvas.left;
			var tipY = e.clientY - rectCanvas.top;
			$('#live_crop_tooltip').text(tooltipText).css({ left: tipX + 'px', top: tipY + 'px' });

			renderLiveCropOverlay();
		});

		var endLiveDrag = function(e) {
			if (isDraggingLiveCrop) {
				isDraggingLiveCrop = false;
				activeHandle = null;
				$('.hyperhdr-crop-handle').removeClass('active');
				$('#live_crop_tooltip').addClass('d-none');
			}
		};

		$('#live_video_crop_overlay .hyperhdr-crop-handle').off('pointerup pointercancel').on('pointerup pointercancel', endLiveDrag);
	}

	$('#btn_crop_apply').off().on('click', function() {
		sendToHyperhdr("video-crop", "", '"crop":{"left":' + curCropL + ',"right":' + curCropR + ',"top":' + curCropT + ',"bottom":' + curCropB + '}');
		showNotification('info', `Applied live crop: Left=${curCropL}px, Top=${curCropT}px, Right=${curCropR}px, Bottom=${curCropB}px`, $.i18n('main_ledsim_btn_cropvideo'));
	});

	$('#btn_crop_save').off().on('click', function() {
		sendToHyperhdr("video-crop", "", '"crop":{"left":' + curCropL + ',"right":' + curCropR + ',"top":' + curCropT + ',"bottom":' + curCropB + '}');
		requestWriteConfig({
			"videoGrabber": {
				"cropLeft": curCropL,
				"cropRight": curCropR,
				"cropTop": curCropT,
				"cropBottom": curCropB
			}
		});
		backupCrop = { l: curCropL, t: curCropT, r: curCropR, b: curCropB };
		if (typeof conf_video_section_editor !== 'undefined' && conf_video_section_editor)
		{
			try {
				conf_video_section_editor.getEditor('root.videoGrabber.cropLeft').setValue(curCropL);
				conf_video_section_editor.getEditor('root.videoGrabber.cropRight').setValue(curCropR);
				conf_video_section_editor.getEditor('root.videoGrabber.cropTop').setValue(curCropT);
				conf_video_section_editor.getEditor('root.videoGrabber.cropBottom').setValue(curCropB);
			} catch(e) {}
		}
		showNotification('success', 'Saved crop settings to Grabber configuration!', $.i18n('main_ledsim_btn_cropvideo'));
	});

	$('#btn_crop_reset').off().on('click', function() {
		curCropL = curCropR = curCropT = curCropB = 0;
		renderLiveCropOverlay();
		sendToHyperhdr("video-crop", "", '"crop":{"left":0,"right":0,"top":0,"bottom":0}');
	});

	$('#btn_crop_close').off().on('click', function() {
		if (backupCrop) {
			sendToHyperhdr("video-crop", "", '"crop":{"left":' + backupCrop.l + ',"right":' + backupCrop.r + ',"top":' + backupCrop.t + ',"bottom":' + backupCrop.b + '}');
		}
		toggleLiveVideoCrop();
	});

	window.startLivePreviewCrop = function() {
		if (dialog == null || typeof dialog === 'undefined')
			getReady();
		dialog.open();
		if (!liveCropActive)
			toggleLiveVideoCrop();
	};

	function updateLedLayout()
	{
		var toolbarHeight = (liveCropActive && $('#live_crop_toolbar').is(':visible')) ? $('#live_crop_toolbar').outerHeight() : 0;
		canvas_height = $('#live_preview_dialog').outerHeight()-$('[data-role=footer]').outerHeight()-$('[data-role=header]').outerHeight()-toolbarHeight-20;
		canvas_width = $('#live_preview_dialog').outerWidth()-20;

		$('#leds_canvas').html("");
		var leds_html = '<canvas id="image_preview_canv" width="'+canvas_width+'" height="'+canvas_height+'"  style="position: absolute; left: 0; top: 0; z-index: 99998;"></canvas>';
		leds_html += '<canvas id="leds_preview_canv" width="'+canvas_width+'" height="'+canvas_height+'"  style="position: absolute; left: 0; top: 0; z-index: 99999;"></canvas>';
		leds_html += '<div id="live_video_crop_overlay" class="hyperhdr-crop-overlay" style="z-index: 100000; display: ' + (liveCropActive ? 'block' : 'none') + ';"></div>';

		$('#leds_canvas').html(leds_html);

		imageCanvasNodeCtx = document.getElementById("image_preview_canv").getContext("2d");
		ledsCanvasNodeCtx = document.getElementById("leds_preview_canv").getContext("2d");
		ledsCanvasNodeCtx.strokeStyle = "rgb(80,80,80)";
		create2dPaths();
		printLedsToCanvas();
		resetImage();
		if (liveCropActive)
		{
			renderLiveCropOverlay();
		}
	};


	function feedWatcher()
	{
		setTimeout(function(){
			if ( window.imageStreamActive )
			{
				var delta = Date.now() - lastFeedFrame;
				if (delta > 2000 && delta < 7000)
					resetImage();

				feedWatcher();
			};
		}, 2000);
	};


	$(window.hyperhdr).on("cmd-image-stream-frame",function(event){
		if (!modalOpened && !window.ledPreviewImageActive)
		{
			requestLedImageStop();
		}
		else if (window.imageStreamActive)
		{
			lastFeedFrame = Date.now();

			var imageData = (event.response);			
			var image = new Image();

			image.onload = function() {
				window.lastImageStreamFrameWidth = image.naturalWidth;
				window.lastImageStreamFrameHeight = image.naturalHeight;
				if (imageCanvasNodeCtx) {
					imageCanvasNodeCtx.drawImage(image, 0, 0, canvas_width, canvas_height);
				}
			};			

			var urlCreator = window.URL || window.webkitURL;
			image.src = urlCreator.createObjectURL(imageData);
		}
	});

	
	$(window.hyperhdr).on("cmd-ledcolors-ledstream-update",function(event){
		if (!modalOpened)
		{
			requestLedColorsStop();
		}
		else
		{
			printLedsToCanvas(event.response.result.leds)
		}
	});

	
	$(window.hyperhdr).on("cmd-settings-update",function(event){
		var obj = event.response.data
		Object.getOwnPropertyNames(obj).forEach(function(val, idx, array) {
			window.serverInfo[val] = obj[val];
	  	});
		leds = window.serverConfig.leds
		updateLedLayout();
	});
	
});
